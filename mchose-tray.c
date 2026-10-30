/*
 * mchose-tray: battery level of a MCHOSE mouse in the system tray.
 *
 * A StatusNotifierItem (what KDE's tray speaks) whose icon is the percentage
 * itself, coloured: green, orange, red, blue while charging, grey while the
 * mouse sleeps (a sleeping mouse does not answer, so the last level is kept).
 * Left click opens mchose-gui.py, right click has a small menu.
 *
 * One status read every --interval seconds: a few 21-byte feature reports on
 * the config interface (0x11 0x06, see mchose.py for the protocol), never on
 * the interface the pointer moves on. The read runs in a worker thread and
 * takes the same lock file as mchose.py, so frames never interleave.
 *
 * Build: cc -O2 -o mchose-tray mchose-tray.c $(pkg-config --cflags --libs gio-2.0 cairo) -lm
 */
#include <cairo.h>
#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <limits.h>
#include <linux/hidraw.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#define LOW 15
#define CRITICAL 5

static const guint16 VIDS[] = {0x3837, 0x5253};
/* From M HUB's model table: each model's own (wired) PID, then the receivers. */
static const guint16 MODEL_PIDS[] = {32, 48, 49, 192, 176, 16405, 177, 16406, 112, 16,
                                     17, 16408, 16419, 16409, 16417, 16401, 16656, 16720, 16400};
static const guint16 RECEIVER_PIDS[] = {4129, 4128, 4106, 4107, 4105, 4097, 161};
static const int SIZES[] = {16, 22, 24, 32, 48, 64};

typedef struct {
    char path[64];
    char name[128];
    gboolean cable;
} Node;

typedef struct {
    gboolean ok;
    int level;
    gboolean charging;
    char link[16];
    char name[128];
    char error[32];
} Reading;

static struct {
    GDBusConnection *bus;
    GMainLoop *loop;
    char bus_name[64];
    GVariant *pixmaps;
    guint revision;
    gboolean polling;
    /* last known state */
    gboolean known, awake, charging;
    int level;
    char name[128], link[16], error[32];
    time_t updated;
    int warned;
} S = {.revision = 1, .warned = 100, .name = "MCHOSE"};

/* ------------------------------------------------------------- the mouse */

static gboolean in_list(const guint16 *list, gsize n, guint v)
{
    for (gsize i = 0; i < n; i++)
        if (list[i] == v)
            return TRUE;
    return FALSE;
}

/* Config collections (usage page 0xFF01) of known MCHOSE PIDs, cable first:
 * on its cable the mouse answers there and the dongle goes quiet. */
static int find_nodes(Node *out, int max)
{
    GDir *dir = g_dir_open("/sys/class/hidraw", 0, NULL);
    if (!dir)
        return 0;
    int n = 0;
    const char *entry;
    while ((entry = g_dir_read_name(dir)) && n < max) {
        g_autofree char *uevent_path = g_strdup_printf("/sys/class/hidraw/%s/device/uevent", entry);
        g_autofree char *desc_path = g_strdup_printf("/sys/class/hidraw/%s/device/report_descriptor", entry);
        g_autofree char *uevent = NULL, *desc = NULL;
        gsize desc_len = 0;
        if (!g_file_get_contents(uevent_path, &uevent, NULL, NULL))
            continue;
        guint bus = 0, vid = 0, pid = 0;
        const char *id = strstr(uevent, "HID_ID=");
        if (!id || sscanf(id + 7, "%x:%x:%x", &bus, &vid, &pid) != 3)
            continue;
        gboolean cable = in_list(MODEL_PIDS, G_N_ELEMENTS(MODEL_PIDS), pid);
        if (!in_list(VIDS, G_N_ELEMENTS(VIDS), vid) ||
            !(cable || in_list(RECEIVER_PIDS, G_N_ELEMENTS(RECEIVER_PIDS), pid)))
            continue;
        if (!g_file_get_contents(desc_path, &desc, &desc_len, NULL) ||
            !memmem(desc, desc_len, "\x06\x01\xff", 3))
            continue;
        Node *node = &out[n++];
        g_snprintf(node->path, sizeof node->path, "/dev/%s", entry);
        node->cable = cable;
        const char *name = strstr(uevent, "HID_NAME=");
        g_strlcpy(node->name, "MCHOSE", sizeof node->name);
        if (name) {
            name += 9;
            if (g_str_has_prefix(name, "RealTek "))
                name += 8;
            gsize len = strcspn(name, "\n");
            g_strlcpy(node->name, name, MIN(len + 1, sizeof node->name));
        }
    }
    g_dir_close(dir);
    for (int i = 1; i < n; i++)   /* cable first, keep the order otherwise */
        for (int j = i; j > 0 && out[j].cable && !out[j - 1].cable; j--) {
            Node t = out[j];
            out[j] = out[j - 1];
            out[j - 1] = t;
        }
    return n;
}

/* 0x11 0x06, re-asked until the reply is a real one: until the mouse has
 * answered over the radio, the dongle hands the request itself back. */
static int request_status(int fd, guint8 payload[19])
{
    guint8 frame[21], buf[21];
    frame[0] = 0x11;
    memset(frame + 1, 0xff, 20);
    frame[1] = 0x06 ^ 0xff;
    for (int attempt = 0; attempt < 8; attempt++) {
        if (ioctl(fd, HIDIOCSFEATURE(sizeof frame), frame) < 0)
            return -errno;
        g_usleep(30000);
        memset(buf, 0, sizeof buf);
        buf[0] = 0x11;
        if (ioctl(fd, HIDIOCGFEATURE(sizeof buf), buf) >= 0 && (buf[1] ^ 0xff) == 0x06 &&
            memcmp(buf, frame, sizeof frame) != 0) {
            gboolean any = FALSE;
            for (int i = 0; i < 19; i++) {
                payload[i] = buf[2 + i] ^ 0xff;
                any |= payload[i] != 0;
            }
            if (any)
                return 1;
        }
        g_usleep(30000);
    }
    return 0;
}

static void read_battery(Reading *r)
{
    Node nodes[8];
    int n = find_nodes(nodes, G_N_ELEMENTS(nodes));
    memset(r, 0, sizeof *r);
    if (n == 0) {
        g_strlcpy(r->error, "no-device", sizeof r->error);
        return;
    }
    g_strlcpy(r->name, nodes[0].name, sizeof r->name);
    g_strlcpy(r->error, "asleep", sizeof r->error);

    const char *run = g_getenv("XDG_RUNTIME_DIR");
    g_autofree char *lock_path = g_build_filename(run ? run : "/tmp", "mchose.lock", NULL);
    int lock = open(lock_path, O_RDWR | O_CREAT | O_CLOEXEC, 0644);
    if (lock >= 0)
        flock(lock, LOCK_EX);
    for (int i = 0; i < n && !r->ok; i++) {
        int fd = open(nodes[i].path, O_RDWR | O_CLOEXEC);
        if (fd < 0) {
            g_strlcpy(r->error, errno == EACCES ? "permission" : "open", sizeof r->error);
            continue;
        }
        guint8 p[19];
        /* right after an idle spell the reply can carry a zero vendor id */
        for (int tries = 0; tries < 3; tries++) {
            if (request_status(fd, p) <= 0)
                break;
            guint vid = p[0] | p[1] << 8;
            if (vid == 0) {
                g_usleep(120000);
                continue;
            }
            if (in_list(VIDS, G_N_ELEMENTS(VIDS), vid) && p[9] <= 100) {
                r->ok = TRUE;
                r->level = p[9];
                r->charging = p[10] != 0;
                static const char *links[] = {"câble", "2.4G", "bluetooth"};
                g_strlcpy(r->link, (p[8] & 7) < 3 ? links[p[8] & 7] : "?", sizeof r->link);
                g_strlcpy(r->name, nodes[i].name, sizeof r->name);
            }
            break;
        }
        close(fd);
    }
    if (lock >= 0)
        close(lock);
}

/* ------------------------------------------------------------- the icon */

static void colour(double rgb[3])
{
    static const double green[] = {0.15, 0.68, 0.38}, orange[] = {0.96, 0.45, 0.0},
                        red[] = {0.85, 0.27, 0.33}, blue[] = {0.24, 0.68, 0.91},
                        grey[] = {0.50, 0.55, 0.55};
    const double *c = !S.known || !S.awake ? grey
                      : S.charging         ? blue
                      : S.level >= 50      ? green
                      : S.level >= 20      ? orange
                                           : red;
    memcpy(rgb, c, sizeof(double) * 3);
}

/* One (w, h, ARGB32 big-endian, not premultiplied) pixmap: the number in white
 * on a rounded tile. */
static GVariant *render(const char *text, const double rgb[3], int size)
{
    cairo_surface_t *surf = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, size, size);
    cairo_t *cr = cairo_create(surf);
    double r = size * 0.22;
    cairo_new_sub_path(cr);
    cairo_arc(cr, size - r, r, r, -M_PI / 2, 0);
    cairo_arc(cr, size - r, size - r, r, 0, M_PI / 2);
    cairo_arc(cr, r, size - r, r, M_PI / 2, M_PI);
    cairo_arc(cr, r, r, r, M_PI, 3 * M_PI / 2);
    cairo_close_path(cr);
    cairo_set_source_rgb(cr, rgb[0], rgb[1], rgb[2]);
    cairo_fill(cr);

    cairo_select_font_face(cr, "sans-serif", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
    double fs = size * (strlen(text) < 3 ? 0.72 : 0.52);
    cairo_text_extents_t ext;
    cairo_set_font_size(cr, fs);
    cairo_text_extents(cr, text, &ext);
    if (ext.width > size * 0.9) {
        cairo_set_font_size(cr, fs * size * 0.9 / ext.width);
        cairo_text_extents(cr, text, &ext);
    }
    cairo_move_to(cr, (size - ext.width) / 2 - ext.x_bearing, (size - ext.height) / 2 - ext.y_bearing);
    cairo_set_source_rgb(cr, 1, 1, 1);
    cairo_show_text(cr, text);
    cairo_destroy(cr);
    cairo_surface_flush(surf);

    const guint8 *data = cairo_image_surface_get_data(surf);
    int stride = cairo_image_surface_get_stride(surf);
    guint8 *out = g_malloc((gsize)size * size * 4), *o = out;
    for (int y = 0; y < size; y++) {
        const guint32 *row = (const guint32 *)(data + y * stride);
        for (int x = 0; x < size; x++) {
            guint32 px = row[x];
            guint a = px >> 24, rr = px >> 16 & 0xff, g = px >> 8 & 0xff, b = px & 0xff;
            if (a && a != 255) {
                rr = rr * 255 / a;
                g = g * 255 / a;
                b = b * 255 / a;
            }
            *o++ = a, *o++ = rr, *o++ = g, *o++ = b;
        }
    }
    cairo_surface_destroy(surf);
    GVariant *bytes = g_variant_new_from_data(G_VARIANT_TYPE_BYTESTRING, out, (gsize)size * size * 4,
                                              TRUE, g_free, out);
    return g_variant_new("(ii@ay)", size, size, bytes);
}

static const char *status_name(void)
{
    return S.known && S.awake && !S.charging && S.level <= LOW ? "NeedsAttention" : "Active";
}

static char *tooltip_text(void)
{
    if (!S.known) {
        if (!strcmp(S.error, "no-device"))
            return g_strdup("Souris introuvable (dongle débranché ?)");
        if (!strcmp(S.error, "permission"))
            return g_strdup("Accès refusé au périphérique (règle udev manquante)");
        return g_strdup("La souris dort : bouge-la pour lire sa batterie");
    }
    char when[16];
    strftime(when, sizeof when, "%H:%M", localtime(&S.updated));
    return g_strdup_printf("Batterie : %d %%%s\n%s · lu à %s%s", S.level, S.charging ? " · en charge" : "",
                           S.link, when, S.awake ? "" : "\n(en veille, dernière valeur connue)");
}

static void emit(const char *path, const char *iface, const char *signal, GVariant *params)
{
    g_dbus_connection_emit_signal(S.bus, NULL, path, iface, signal, params, NULL);
}

static void redraw(void)
{
    char text[8];
    double rgb[3];
    if (S.known)
        g_snprintf(text, sizeof text, "%d", S.level);
    else
        g_strlcpy(text, "–", sizeof text);
    colour(rgb);
    GVariantBuilder b;
    g_variant_builder_init(&b, G_VARIANT_TYPE("a(iiay)"));
    for (gsize i = 0; i < G_N_ELEMENTS(SIZES); i++)
        g_variant_builder_add_value(&b, render(text, rgb, SIZES[i]));
    if (S.pixmaps)
        g_variant_unref(S.pixmaps);
    S.pixmaps = g_variant_ref_sink(g_variant_builder_end(&b));

    emit("/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewIcon", NULL);
    emit("/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewAttentionIcon", NULL);
    emit("/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewToolTip", NULL);
    emit("/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewStatus", g_variant_new("(s)", status_name()));
    emit("/MenuBar", "com.canonical.dbusmenu", "LayoutUpdated", g_variant_new("(ui)", ++S.revision, 0));
}

static void notify(const char *summary, const char *body)
{
    GVariantBuilder hints;
    g_variant_builder_init(&hints, G_VARIANT_TYPE("a{sv}"));
    g_variant_builder_add(&hints, "{sv}", "urgency", g_variant_new_byte(2));
    g_dbus_connection_call(S.bus, "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                           "org.freedesktop.Notifications", "Notify",
                           g_variant_new("(susss@as@a{sv}i)", "MCHOSE", 0, "input-mouse", summary, body,
                                         g_variant_new_strv(NULL, 0), g_variant_builder_end(&hints), -1),
                           NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

static void maybe_warn(void)
{
    if (S.charging) {
        S.warned = 100;
        return;
    }
    for (int limit = CRITICAL; limit <= LOW; limit += LOW - CRITICAL) {
        if (S.level <= limit && limit < S.warned) {
            S.warned = limit;
            g_autofree char *summary = g_strdup_printf("Batterie faible : %d %%", S.level);
            g_autofree char *body = g_strdup_printf("%s : pense à la recharger.", S.name);
            notify(summary, body);
            return;
        }
    }
}

/* ------------------------------------------------------------- polling */

static void poll_worker(GTask *task, gpointer src, gpointer data, GCancellable *c)
{
    Reading *r = g_new(Reading, 1);
    read_battery(r);
    g_task_return_pointer(task, r, g_free);
}

static void poll_done(GObject *src, GAsyncResult *res, gpointer data)
{
    g_autofree Reading *r = g_task_propagate_pointer(G_TASK(res), NULL);
    S.polling = FALSE;
    if (!r)
        return;
    if (r->name[0])
        g_strlcpy(S.name, r->name, sizeof S.name);
    if (r->ok) {
        S.known = S.awake = TRUE;
        S.level = r->level;
        S.charging = r->charging;
        g_strlcpy(S.link, r->link, sizeof S.link);
        S.updated = time(NULL);
        S.error[0] = 0;
        maybe_warn();
    } else {
        S.awake = FALSE;
        g_strlcpy(S.error, r->error, sizeof S.error);
    }
    redraw();
}

static void poll_now(void)
{
    if (S.polling)
        return;
    S.polling = TRUE;
    GTask *task = g_task_new(NULL, NULL, poll_done, NULL);
    g_task_run_in_thread(task, poll_worker);
    g_object_unref(task);
}

static gboolean tick(gpointer data)
{
    poll_now();
    return G_SOURCE_CONTINUE;
}

static void open_settings(void)
{
    g_autofree char *exe = realpath("/proc/self/exe", NULL);
    g_autofree char *dir = g_path_get_dirname(exe ? exe : ".");
    g_autofree char *gui = g_build_filename(dir, "mchose-gui.py", NULL);
    char *argv[] = {"python3", gui, NULL};
    g_autoptr(GError) err = NULL;
    if (!g_spawn_async(NULL, argv, NULL, G_SPAWN_SEARCH_PATH | G_SPAWN_STDOUT_TO_DEV_NULL |
                       G_SPAWN_STDERR_TO_DEV_NULL, NULL, NULL, NULL, &err))
        g_warning("mchose-gui: %s", err->message);
}

/* ------------------------------------------------------- StatusNotifierItem */

static const char SNI_XML[] =
    "<node><interface name='org.kde.StatusNotifierItem'>"
    "<property name='Category' type='s' access='read'/>"
    "<property name='Id' type='s' access='read'/>"
    "<property name='Title' type='s' access='read'/>"
    "<property name='Status' type='s' access='read'/>"
    "<property name='WindowId' type='i' access='read'/>"
    "<property name='IconName' type='s' access='read'/>"
    "<property name='IconPixmap' type='a(iiay)' access='read'/>"
    "<property name='AttentionIconPixmap' type='a(iiay)' access='read'/>"
    "<property name='ToolTip' type='(sa(iiay)ss)' access='read'/>"
    "<property name='ItemIsMenu' type='b' access='read'/>"
    "<property name='Menu' type='o' access='read'/>"
    "<method name='ContextMenu'><arg name='x' type='i' direction='in'/><arg name='y' type='i' direction='in'/></method>"
    "<method name='Activate'><arg name='x' type='i' direction='in'/><arg name='y' type='i' direction='in'/></method>"
    "<method name='SecondaryActivate'><arg name='x' type='i' direction='in'/><arg name='y' type='i' direction='in'/></method>"
    "<method name='Scroll'><arg name='delta' type='i' direction='in'/><arg name='orientation' type='s' direction='in'/></method>"
    "<signal name='NewTitle'/><signal name='NewIcon'/><signal name='NewAttentionIcon'/><signal name='NewToolTip'/>"
    "<signal name='NewStatus'><arg name='status' type='s'/></signal>"
    "</interface></node>";

static GVariant *sni_get(GDBusConnection *c, const char *sender, const char *path, const char *iface,
                         const char *prop, GError **err, gpointer data)
{
    if (!strcmp(prop, "Category"))
        return g_variant_new_string("Hardware");
    if (!strcmp(prop, "Id"))
        return g_variant_new_string("mchose-battery");
    if (!strcmp(prop, "Title")) {
        g_autofree char *t = g_strdup_printf("%s - batterie", S.name);
        return g_variant_new_string(t);
    }
    if (!strcmp(prop, "Status"))
        return g_variant_new_string(status_name());
    if (!strcmp(prop, "WindowId"))
        return g_variant_new_int32(0);
    if (!strcmp(prop, "IconName"))
        return g_variant_new_string("");
    if (!strcmp(prop, "IconPixmap") || !strcmp(prop, "AttentionIconPixmap"))
        return g_variant_ref(S.pixmaps);
    if (!strcmp(prop, "ToolTip")) {
        g_autofree char *text = tooltip_text();
        return g_variant_new("(s@a(iiay)ss)", "", g_variant_new_array(G_VARIANT_TYPE("(iiay)"), NULL, 0),
                             S.name, text);
    }
    if (!strcmp(prop, "ItemIsMenu"))
        return g_variant_new_boolean(FALSE);
    if (!strcmp(prop, "Menu"))
        return g_variant_new_object_path("/MenuBar");
    return NULL;
}

static void sni_call(GDBusConnection *c, const char *sender, const char *path, const char *iface,
                     const char *method, GVariant *params, GDBusMethodInvocation *inv, gpointer data)
{
    if (!strcmp(method, "Activate"))
        open_settings();
    else if (!strcmp(method, "SecondaryActivate"))
        poll_now();
    g_dbus_method_invocation_return_value(inv, NULL);
}

/* ------------------------------------------------------- com.canonical.dbusmenu */

static const char MENU_XML[] =
    "<node><interface name='com.canonical.dbusmenu'>"
    "<property name='Version' type='u' access='read'/>"
    "<property name='TextDirection' type='s' access='read'/>"
    "<property name='Status' type='s' access='read'/>"
    "<property name='IconThemePath' type='as' access='read'/>"
    "<method name='GetLayout'><arg type='i' direction='in'/><arg type='i' direction='in'/>"
    "<arg type='as' direction='in'/><arg type='u' direction='out'/><arg type='(ia{sv}av)' direction='out'/></method>"
    "<method name='GetGroupProperties'><arg type='ai' direction='in'/><arg type='as' direction='in'/>"
    "<arg type='a(ia{sv})' direction='out'/></method>"
    "<method name='GetProperty'><arg type='i' direction='in'/><arg type='s' direction='in'/>"
    "<arg type='v' direction='out'/></method>"
    "<method name='Event'><arg type='i' direction='in'/><arg type='s' direction='in'/>"
    "<arg type='v' direction='in'/><arg type='u' direction='in'/></method>"
    "<method name='EventGroup'><arg type='a(isvu)' direction='in'/><arg type='ai' direction='out'/></method>"
    "<method name='AboutToShow'><arg type='i' direction='in'/><arg type='b' direction='out'/></method>"
    "<method name='AboutToShowGroup'><arg type='ai' direction='in'/><arg type='ai' direction='out'/>"
    "<arg type='ai' direction='out'/></method>"
    "<signal name='ItemsPropertiesUpdated'><arg type='a(ia{sv})'/><arg type='a(ias)'/></signal>"
    "<signal name='LayoutUpdated'><arg type='u'/><arg type='i'/></signal>"
    "<signal name='ItemActivationRequested'><arg type='i'/><arg type='u'/></signal>"
    "</interface></node>";

enum { ITEM_LEVEL = 1, ITEM_SEP1, ITEM_SETTINGS, ITEM_REFRESH, ITEM_SEP2, ITEM_QUIT, ITEM_LAST };

static GVariant *item_props(int id)
{
    GVariantBuilder b;
    g_variant_builder_init(&b, G_VARIANT_TYPE("a{sv}"));
    switch (id) {
    case ITEM_LEVEL: {
        g_autofree char *label = S.known ? g_strdup_printf("Batterie : %d %%%s", S.level,
                                                           S.charging ? " (en charge)" : "")
                                         : g_strdup("Batterie : ?");
        g_variant_builder_add(&b, "{sv}", "label", g_variant_new_string(label));
        g_variant_builder_add(&b, "{sv}", "enabled", g_variant_new_boolean(FALSE));
        break;
    }
    case ITEM_SEP1:
    case ITEM_SEP2:
        g_variant_builder_add(&b, "{sv}", "type", g_variant_new_string("separator"));
        break;
    case ITEM_SETTINGS:
        g_variant_builder_add(&b, "{sv}", "label", g_variant_new_string("Réglages de la souris…"));
        g_variant_builder_add(&b, "{sv}", "icon-name", g_variant_new_string("configure"));
        break;
    case ITEM_REFRESH:
        g_variant_builder_add(&b, "{sv}", "label", g_variant_new_string("Actualiser maintenant"));
        g_variant_builder_add(&b, "{sv}", "icon-name", g_variant_new_string("view-refresh"));
        break;
    case ITEM_QUIT:
        g_variant_builder_add(&b, "{sv}", "label", g_variant_new_string("Quitter"));
        g_variant_builder_add(&b, "{sv}", "icon-name", g_variant_new_string("application-exit"));
        break;
    case 0:
        g_variant_builder_add(&b, "{sv}", "children-display", g_variant_new_string("submenu"));
        break;
    }
    return g_variant_builder_end(&b);
}

static void menu_clicked(int id)
{
    if (id == ITEM_SETTINGS)
        open_settings();
    else if (id == ITEM_REFRESH)
        poll_now();
    else if (id == ITEM_QUIT)
        g_main_loop_quit(S.loop);
}

static GVariant *menu_get(GDBusConnection *c, const char *sender, const char *path, const char *iface,
                          const char *prop, GError **err, gpointer data)
{
    if (!strcmp(prop, "Version"))
        return g_variant_new_uint32(3);
    if (!strcmp(prop, "TextDirection"))
        return g_variant_new_string("ltr");
    if (!strcmp(prop, "Status"))
        return g_variant_new_string("normal");
    if (!strcmp(prop, "IconThemePath"))
        return g_variant_new_strv(NULL, 0);
    return NULL;
}

static void menu_call(GDBusConnection *c, const char *sender, const char *path, const char *iface,
                      const char *method, GVariant *params, GDBusMethodInvocation *inv, gpointer data)
{
    if (!strcmp(method, "GetLayout")) {
        GVariantBuilder kids;
        g_variant_builder_init(&kids, G_VARIANT_TYPE("av"));
        for (int id = ITEM_LEVEL; id < ITEM_LAST; id++)
            g_variant_builder_add(&kids, "v", g_variant_new("(i@a{sv}@av)", id, item_props(id),
                                                             g_variant_new_array(G_VARIANT_TYPE_VARIANT, NULL, 0)));
        GVariant *root = g_variant_new("(i@a{sv}@av)", 0, item_props(0), g_variant_builder_end(&kids));
        g_dbus_method_invocation_return_value(inv, g_variant_new("(u@(ia{sv}av))", S.revision, root));
    } else if (!strcmp(method, "GetGroupProperties")) {
        g_autoptr(GVariant) ids = g_variant_get_child_value(params, 0);
        gsize n = 0;
        const gint32 *want = g_variant_get_fixed_array(ids, &n, sizeof(gint32));
        GVariantBuilder b;
        g_variant_builder_init(&b, G_VARIANT_TYPE("a(ia{sv})"));
        for (int id = ITEM_LEVEL; id < ITEM_LAST; id++) {
            gboolean hit = n == 0;
            for (gsize i = 0; i < n && !hit; i++)
                hit = want[i] == id;
            if (hit)
                g_variant_builder_add(&b, "(i@a{sv})", id, item_props(id));
        }
        g_dbus_method_invocation_return_value(inv, g_variant_new("(a(ia{sv}))", &b));
    } else if (!strcmp(method, "GetProperty")) {
        gint32 id;
        const char *name;
        g_variant_get(params, "(i&s)", &id, &name);
        g_autoptr(GVariant) props = g_variant_ref_sink(item_props(id));
        GVariant *v = g_variant_lookup_value(props, name, NULL);
        g_dbus_method_invocation_return_value(inv, g_variant_new("(v)", v ? v : g_variant_new_string("")));
        if (v)
            g_variant_unref(v);
    } else if (!strcmp(method, "Event")) {
        gint32 id;
        const char *event;
        g_variant_get(params, "(i&svu)", &id, &event, NULL, NULL);
        if (!strcmp(event, "clicked"))
            menu_clicked(id);
        g_dbus_method_invocation_return_value(inv, NULL);
    } else if (!strcmp(method, "EventGroup")) {
        g_autoptr(GVariantIter) it = NULL;
        g_variant_get(params, "(a(isvu))", &it);
        gint32 id;
        const char *event;
        GVariant *v;
        while (g_variant_iter_next(it, "(i&svu)", &id, &event, &v, NULL)) {
            if (!strcmp(event, "clicked"))
                menu_clicked(id);
            g_variant_unref(v);
        }
        g_dbus_method_invocation_return_value(inv, g_variant_new_parsed("(@ai [],)"));
    } else if (!strcmp(method, "AboutToShow")) {
        g_dbus_method_invocation_return_value(inv, g_variant_new("(b)", FALSE));
    } else if (!strcmp(method, "AboutToShowGroup")) {
        g_dbus_method_invocation_return_value(inv, g_variant_new_parsed("(@ai [], @ai [])"));
    } else {
        g_dbus_method_invocation_return_value(inv, NULL);
    }
}

/* ------------------------------------------------------------------ main */

static void watcher_appeared(GDBusConnection *c, const char *name, const char *owner, gpointer data)
{
    g_dbus_connection_call(c, name, "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                           "RegisterStatusNotifierItem", g_variant_new("(s)", S.bus_name), NULL,
                           G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

int main(int argc, char **argv)
{
    gint interval = 60;
    GOptionEntry entries[] = {
        {"interval", 'i', 0, G_OPTION_ARG_INT, &interval, "secondes entre deux lectures (défaut 60)", "S"},
        {NULL}};
    g_autoptr(GOptionContext) ctx = g_option_context_new("- batterie de la souris MCHOSE dans la zone de notification");
    g_option_context_add_main_entries(ctx, entries, NULL);
    g_autoptr(GError) err = NULL;
    if (!g_option_context_parse(ctx, &argc, &argv, &err)) {
        g_printerr("%s\n", err->message);
        return 1;
    }
    interval = MAX(interval, 10);

    S.bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &err);
    if (!S.bus) {
        g_printerr("bus de session : %s\n", err->message);
        return 1;
    }
    g_snprintf(S.bus_name, sizeof S.bus_name, "org.kde.StatusNotifierItem-%d-1", getpid());
    g_autoptr(GDBusNodeInfo) sni = g_dbus_node_info_new_for_xml(SNI_XML, NULL);
    g_autoptr(GDBusNodeInfo) menu = g_dbus_node_info_new_for_xml(MENU_XML, NULL);
    static const GDBusInterfaceVTable sni_vt = {.method_call = sni_call, .get_property = sni_get};
    static const GDBusInterfaceVTable menu_vt = {.method_call = menu_call, .get_property = menu_get};
    g_dbus_connection_register_object(S.bus, "/StatusNotifierItem", sni->interfaces[0], &sni_vt, NULL, NULL, NULL);
    g_dbus_connection_register_object(S.bus, "/MenuBar", menu->interfaces[0], &menu_vt, NULL, NULL, NULL);
    g_bus_own_name_on_connection(S.bus, S.bus_name, G_BUS_NAME_OWNER_FLAGS_NONE, NULL, NULL, NULL, NULL);
    g_bus_watch_name_on_connection(S.bus, "org.kde.StatusNotifierWatcher", G_BUS_NAME_WATCHER_FLAGS_NONE,
                                   watcher_appeared, NULL, NULL, NULL);

    redraw();
    poll_now();
    g_timeout_add_seconds(interval, tick, NULL);
    S.loop = g_main_loop_new(NULL, FALSE);
    g_main_loop_run(S.loop);
    return 0;
}
