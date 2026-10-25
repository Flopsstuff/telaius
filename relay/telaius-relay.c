/*
 * telaius-relay: carries VoLTE (and CS) call audio between USB if4 and the module's ADSP.
 *
 * Runs on the SIM7600 module (MDM9607 Linux). SIMCom's own USB audio (AT+CPCMREG) is 8 kHz only on
 * the working firmware, so this relay uses Qualcomm's incall routes instead:
 *   far end -> host:  "MultiMedia1 Mixer VOC_REC_DL" + capture hw:0,0 @48 kHz -> 16 kHz -> /dev/ttyGS0
 *   host -> far end:  /dev/ttyGS0 -> playback hw:0,13 @16 kHz + "Incall_Music Audio Mixer MultiMedia2"
 * /dev/ttyGS0 is USB interface 4 on the host, carrying raw s16le mono 16 kHz in both directions.
 *
 * The host drives the session implicitly: audio arriving on ttyGS0 starts it, 1.5 s without data stops it.
 *
 * Usage: telaius-relay [-l] [-m] [-v]
 *   -l  loopback: echo ttyGS0 back to the host (USB plumbing test, no ALSA)
 *   -m  mute the onboard mic in the VoLTE/CS uplink while a session runs
 *   -v  verbose stats every 5 s
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include <sound/asound.h>

#define TTY_PATH "/dev/ttyGS0"
#define CAP_DEV "/dev/snd/pcmC0D0c"   /* MultiMedia1 */
#define PLAY_DEV "/dev/snd/pcmC0D13p" /* MultiMedia2 */
#define CAP_RATE 48000
#define USB_RATE 16000
#define FRAME_MS 20
#define USB_FRAME (USB_RATE * FRAME_MS / 1000) /* 320 samples */
#define CAP_FRAME (CAP_RATE * FRAME_MS / 1000) /* 960 samples */
#define IDLE_STOP_MS 1500
#define UL_TARGET (3 * USB_FRAME)  /* jitter buffer target: 60 ms */
#define UL_MAX (8 * USB_FRAME)     /* above 160 ms, drop back to the target */
#define RING (64 * USB_FRAME)
/* The q6 voice record/incall music paths only move small DSP buffers: use what arec/aplay pick
 * (capture 160 frames x 8, playback 64 frames x 8). A 20 ms period is accepted by hw_params but the
 * capture then times out with EIO. Transfers stay 20 ms; ALSA splits them across periods. */
#define CAP_PERIOD 160
#define CAP_PERIODS 8
#define PLAY_PERIOD 64
#define PLAY_PERIODS 8

static int opt_loop, opt_mute, opt_verbose;
static int tty_fd = -1;
static volatile int running = 1;

static void logmsg(const char *fmt, ...)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    struct tm tm;
    localtime_r(&ts.tv_sec, &tm);
    fprintf(stderr, "[%02d:%02d:%02d.%03ld] ", tm.tm_hour, tm.tm_min, tm.tm_sec, ts.tv_nsec / 1000000);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

static int64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void amix_str(const char *ctl, const char *value)
{
    char cmd[256];
    snprintf(cmd, sizeof cmd, "amix '%s' '%s' >/dev/null 2>&1", ctl, value);
    if (system(cmd) != 0)
        logmsg("amix failed: %s %s", ctl, value);
}

static void amix(const char *ctl, int value)
{
    char v[16];
    snprintf(v, sizeof v, "%d", value);
    amix_str(ctl, v);
}

/* ---------- minimal ALSA PCM over ioctl (as tinyalsa does) ---------- */

static void mask_set(struct snd_pcm_hw_params *p, int n, unsigned bit)
{
    struct snd_mask *m = &p->masks[n - SNDRV_PCM_HW_PARAM_FIRST_MASK];
    memset(m, 0, sizeof *m);
    m->bits[bit >> 5] |= 1u << (bit & 31);
}

static void interval_set(struct snd_pcm_hw_params *p, int n, unsigned v)
{
    struct snd_interval *i = &p->intervals[n - SNDRV_PCM_HW_PARAM_FIRST_INTERVAL];
    i->min = i->max = v;
    i->integer = 1;
}

static void hw_params_any(struct snd_pcm_hw_params *p)
{
    memset(p, 0, sizeof *p);
    for (int n = SNDRV_PCM_HW_PARAM_FIRST_MASK; n <= SNDRV_PCM_HW_PARAM_LAST_MASK; n++)
        memset(&p->masks[n - SNDRV_PCM_HW_PARAM_FIRST_MASK], 0xff, sizeof(struct snd_mask));
    for (int n = SNDRV_PCM_HW_PARAM_FIRST_INTERVAL; n <= SNDRV_PCM_HW_PARAM_LAST_INTERVAL; n++) {
        struct snd_interval *i = &p->intervals[n - SNDRV_PCM_HW_PARAM_FIRST_INTERVAL];
        i->min = 0;
        i->max = ~0u;
    }
    p->rmask = ~0u;
    p->info = ~0u;
}

struct pcm {
    int fd;
    int capture;
    unsigned period, periods;
};

static int pcm_open(struct pcm *pcm, const char *path, int capture, unsigned rate, unsigned period, unsigned periods)
{
    pcm->fd = open(path, O_RDWR);
    if (pcm->fd < 0) {
        logmsg("open %s: %s", path, strerror(errno));
        return -1;
    }
    pcm->capture = capture;
    pcm->period = period;
    pcm->periods = periods;

    struct snd_pcm_hw_params hp;
    hw_params_any(&hp);
    mask_set(&hp, SNDRV_PCM_HW_PARAM_ACCESS, SNDRV_PCM_ACCESS_RW_INTERLEAVED);
    mask_set(&hp, SNDRV_PCM_HW_PARAM_FORMAT, SNDRV_PCM_FORMAT_S16_LE);
    mask_set(&hp, SNDRV_PCM_HW_PARAM_SUBFORMAT, SNDRV_PCM_SUBFORMAT_STD);
    interval_set(&hp, SNDRV_PCM_HW_PARAM_SAMPLE_BITS, 16);
    interval_set(&hp, SNDRV_PCM_HW_PARAM_FRAME_BITS, 16);
    interval_set(&hp, SNDRV_PCM_HW_PARAM_CHANNELS, 1);
    interval_set(&hp, SNDRV_PCM_HW_PARAM_RATE, rate);
    interval_set(&hp, SNDRV_PCM_HW_PARAM_PERIOD_SIZE, period);
    interval_set(&hp, SNDRV_PCM_HW_PARAM_PERIODS, periods);
    if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_HW_PARAMS, &hp) < 0) {
        logmsg("%s hw_params (rate %u period %u x%u): %s", path, rate, period, periods, strerror(errno));
        goto fail;
    }

    struct snd_pcm_sw_params sp;
    memset(&sp, 0, sizeof sp);
    unsigned long buffer = period * periods;
    sp.tstamp_mode = SNDRV_PCM_TSTAMP_NONE;
    sp.period_step = 1;
    sp.avail_min = period;  /* reads/writes of 20 ms block until enough frames are available */
    sp.start_threshold = capture ? 1 : period * 2;
    sp.stop_threshold = buffer;
    sp.boundary = buffer;
    while (sp.boundary * 2 <= 0x7fffffffUL - buffer)
        sp.boundary *= 2;
    if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_SW_PARAMS, &sp) < 0) {
        logmsg("%s sw_params: %s", path, strerror(errno));
        goto fail;
    }
    if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_PREPARE) < 0) {
        logmsg("%s prepare: %s", path, strerror(errno));
        goto fail;
    }
    if (capture && ioctl(pcm->fd, SNDRV_PCM_IOCTL_START) < 0) {
        logmsg("%s start: %s", path, strerror(errno));
        goto fail;
    }
    return 0;
fail:
    close(pcm->fd);
    pcm->fd = -1;
    return -1;
}

static void pcm_close(struct pcm *pcm)
{
    if (pcm->fd >= 0) {
        ioctl(pcm->fd, SNDRV_PCM_IOCTL_DROP);
        close(pcm->fd);
    }
    pcm->fd = -1;
}

/* Transfer exactly `frames`; recover once from an xrun. Returns 0 or -errno. */
static int pcm_xfer(struct pcm *pcm, int16_t *buf, unsigned frames, unsigned *xruns)
{
    for (int attempt = 0; attempt < 2; attempt++) {
        struct snd_xferi x = {.result = 0, .buf = buf, .frames = frames};
        int r = ioctl(pcm->fd, pcm->capture ? SNDRV_PCM_IOCTL_READI_FRAMES : SNDRV_PCM_IOCTL_WRITEI_FRAMES, &x);
        if (r == 0)
            return 0;
        int err = errno;
        if (err != EPIPE)
            return -err;
        (*xruns)++;
        ioctl(pcm->fd, SNDRV_PCM_IOCTL_PREPARE);
        if (pcm->capture)
            ioctl(pcm->fd, SNDRV_PCM_IOCTL_START);
    }
    return -EPIPE;
}

/* ---------- 48 kHz -> 16 kHz decimator (windowed-sinc low-pass, cutoff 7.2 kHz) ---------- */

#define TAPS 48
static float fir[TAPS];

static void fir_init(void)
{
    double fc = 7200.0 / CAP_RATE, sum = 0;
    for (int i = 0; i < TAPS; i++) {
        double m = i - (TAPS - 1) / 2.0;
        double sinc = m == 0 ? 2 * fc : sin(2 * M_PI * fc * m) / (M_PI * m);
        double win = 0.54 - 0.46 * cos(2 * M_PI * i / (TAPS - 1));
        fir[i] = (float)(sinc * win);
        sum += fir[i];
    }
    for (int i = 0; i < TAPS; i++)
        fir[i] /= (float)sum;
}

struct decimator {
    float hist[TAPS + CAP_FRAME];
};

static void decimate(struct decimator *d, const int16_t *in, int16_t *out)
{
    memmove(d->hist, d->hist + CAP_FRAME, TAPS * sizeof(float));
    for (int i = 0; i < CAP_FRAME; i++)
        d->hist[TAPS + i] = in[i];
    for (int o = 0; o < USB_FRAME; o++) {
        const float *x = d->hist + 3 * o + 3;
        float acc = 0;
        for (int k = 0; k < TAPS; k++)
            acc += fir[k] * x[k];
        out[o] = acc > 32767 ? 32767 : acc < -32768 ? -32768 : (int16_t)lrintf(acc);
    }
}

/* ---------- uplink ring (host -> far end) ---------- */

static int16_t ring[RING];
static unsigned ring_r, ring_w; /* in samples, monotonically increasing */
static pthread_mutex_t ring_lock = PTHREAD_MUTEX_INITIALIZER;
static uint8_t ul_odd;          /* carries an odd byte between tty reads */
static int ul_has_odd;

static void ring_push(const uint8_t *buf, size_t n)
{
    pthread_mutex_lock(&ring_lock);
    size_t i = 0;
    if (ul_has_odd && n) {
        int16_t s = (int16_t)(ul_odd | (buf[0] << 8));
        ring[ring_w++ % RING] = s;
        ul_has_odd = 0;
        i = 1;
    }
    for (; i + 1 < n; i += 2)
        ring[ring_w++ % RING] = (int16_t)(buf[i] | (buf[i + 1] << 8));
    if (i < n) {
        ul_odd = buf[i];
        ul_has_odd = 1;
    }
    if (ring_w - ring_r > RING)
        ring_r = ring_w - RING;
    pthread_mutex_unlock(&ring_lock);
}

/* ---------- session ---------- */

struct stats {
    unsigned long ul_bytes, dl_bytes, ul_underruns, ul_drops, dl_drops, play_xruns, cap_xruns, cap_errors;
} st;

static volatile int session_on;
static pthread_t th_play, th_cap;

static void *play_thread(void *arg)
{
    (void)arg;
    struct pcm pcm;
    int16_t frame[USB_FRAME];
    while (session_on) {
        if (pcm_open(&pcm, PLAY_DEV, 0, USB_RATE, PLAY_PERIOD, PLAY_PERIODS) < 0) {
            usleep(300000);
            continue;
        }
        logmsg("playback %s @%d Hz running", PLAY_DEV, USB_RATE);
        while (session_on) {
            pthread_mutex_lock(&ring_lock);
            unsigned avail = ring_w - ring_r;
            if (avail > UL_MAX) {
                ring_r = ring_w - UL_TARGET;
                st.ul_drops++;
                avail = UL_TARGET;
            }
            if (avail >= USB_FRAME) {
                for (int i = 0; i < USB_FRAME; i++)
                    frame[i] = ring[ring_r++ % RING];
            } else {
                memset(frame, 0, sizeof frame);
                st.ul_underruns++;
            }
            pthread_mutex_unlock(&ring_lock);
            int r = pcm_xfer(&pcm, frame, USB_FRAME, (unsigned *)&st.play_xruns);
            if (r < 0) {
                logmsg("playback write: %s", strerror(-r));
                break;
            }
        }
        pcm_close(&pcm);
    }
    return NULL;
}

/* Downlink to the host. A non-blocking tty write can be partial; dropping the rest would shift every
 * later sample by a byte, so the remainder is kept and sent first next time. If the host stops reading,
 * whole frames (an even number of bytes) are dropped from the front to bound the backlog. */
static uint8_t dl_pend[8 * USB_FRAME * 2];
static size_t dl_pend_len;

static void dl_send(const uint8_t *frame, size_t len)
{
    if (dl_pend_len + len > sizeof dl_pend) {
        size_t drop = USB_FRAME * 2;
        while (dl_pend_len + len - drop > sizeof dl_pend)
            drop += USB_FRAME * 2;
        if (drop > dl_pend_len)
            drop = dl_pend_len - dl_pend_len % 2;
        memmove(dl_pend, dl_pend + drop, dl_pend_len - drop);
        dl_pend_len -= drop;
        st.dl_drops++;
    }
    memcpy(dl_pend + dl_pend_len, frame, len);
    dl_pend_len += len;
    ssize_t w = write(tty_fd, dl_pend, dl_pend_len);
    if (w > 0) {
        memmove(dl_pend, dl_pend + w, dl_pend_len - w);
        dl_pend_len -= w;
        st.dl_bytes += w;
    }
}

static void *cap_thread(void *arg)
{
    (void)arg;
    static struct decimator dec;
    struct pcm pcm;
    int16_t in[CAP_FRAME], out[USB_FRAME];
    while (session_on) {
        if (pcm_open(&pcm, CAP_DEV, 1, CAP_RATE, CAP_PERIOD, CAP_PERIODS) < 0) {
            st.cap_errors++;
            usleep(300000);
            continue;
        }
        memset(&dec, 0, sizeof dec);
        dl_pend_len = 0;
        logmsg("capture %s @%d Hz running", CAP_DEV, CAP_RATE);
        while (session_on) {
            int r = pcm_xfer(&pcm, in, CAP_FRAME, (unsigned *)&st.cap_xruns);
            if (r < 0) {
                /* EIO here typically means no active voice session yet */
                logmsg("capture read: %s", strerror(-r));
                st.cap_errors++;
                break;
            }
            decimate(&dec, in, out);
            dl_send((const uint8_t *)out, sizeof out);
        }
        pcm_close(&pcm);
        if (session_on)
            usleep(300000);
    }
    return NULL;
}

static void mixers(int on)
{
    amix("MultiMedia1 Mixer VOC_REC_DL", on);
    amix("Incall_Music Audio Mixer MultiMedia2", on);
    if (opt_mute && on) {
        /* The onboard NAU8810 codec feeds the voice uplink over AUX PCM. Removing that route from the
         * VoLTE TX mixer also kills incall music, so put the codec to sleep instead (alsa_daemon does
         * the same at call end): AUX PCM keeps clocking zeros and the DSP TX path stays up. */
        amix_str("SIMCOM Switch Device", "Sleep");
    }
}

static void session_start(void)
{
    logmsg("session start");
    memset(&st, 0, sizeof st);
    /* bytes left in the tty output from the previous session would put the new stream off by one */
    tcflush(tty_fd, TCOFLUSH);
    pthread_mutex_lock(&ring_lock);
    ring_r = ring_w;
    ul_has_odd = 0;
    pthread_mutex_unlock(&ring_lock);
    mixers(1);
    session_on = 1;
    pthread_create(&th_play, NULL, play_thread, NULL);
    pthread_create(&th_cap, NULL, cap_thread, NULL);
}

static void log_stats(const char *tag)
{
    logmsg("%s: ul %lu B (underruns %lu, drops %lu), dl %lu B (drops %lu), xruns play %lu cap %lu, cap errors %lu", tag,
           st.ul_bytes, st.ul_underruns, st.ul_drops, st.dl_bytes, st.dl_drops, st.play_xruns, st.cap_xruns,
           st.cap_errors);
}

static void session_stop(void)
{
    session_on = 0;
    pthread_join(th_play, NULL);
    pthread_join(th_cap, NULL);
    mixers(0);
    log_stats("session stop");
}

static int tty_open(void)
{
    int fd = open(TTY_PATH, O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (fd < 0)
        return -1;
    struct termios t;
    if (tcgetattr(fd, &t) == 0) {
        cfmakeraw(&t); /* no echo, no line discipline: raw PCM both ways */
        tcsetattr(fd, TCSANOW, &t);
    }
    tcflush(fd, TCIOFLUSH);
    return fd;
}

static void on_signal(int sig)
{
    (void)sig;
    running = 0;
}

int main(int argc, char **argv)
{
    int c;
    while ((c = getopt(argc, argv, "lmv")) != -1) {
        if (c == 'l')
            opt_loop = 1;
        else if (c == 'm')
            opt_mute = 1;
        else if (c == 'v')
            opt_verbose = 1;
        else {
            fprintf(stderr, "usage: %s [-l] [-m] [-v]\n", argv[0]);
            return 2;
        }
    }
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    setvbuf(stderr, NULL, _IOLBF, 0);
    fir_init();

    while ((tty_fd = tty_open()) < 0) {
        logmsg("waiting for %s: %s", TTY_PATH, strerror(errno));
        sleep(2);
    }
    logmsg("telaius-relay up (%s%s)", opt_loop ? "loopback" : "relay", opt_mute ? ", mic mute" : "");

    uint8_t buf[4096];
    int64_t last_rx = 0, last_stats = now_ms();
    while (running) {
        struct pollfd p = {.fd = tty_fd, .events = POLLIN};
        int pr = poll(&p, 1, 200);
        int64_t t = now_ms();
        if (pr > 0 && (p.revents & POLLIN)) {
            ssize_t n = read(tty_fd, buf, sizeof buf);
            if (n > 0) {
                last_rx = t;
                st.ul_bytes += n;
                if (opt_loop) {
                    if (write(tty_fd, buf, n) != n)
                        st.dl_drops++;
                    else
                        st.dl_bytes += n;
                } else {
                    if (!session_on)
                        session_start();
                    ring_push(buf, n);
                }
            }
        } else if (pr > 0 && (p.revents & (POLLERR | POLLHUP))) {
            usleep(100000); /* host not attached; keep waiting */
        }
        if (session_on && t - last_rx > IDLE_STOP_MS)
            session_stop();
        if (opt_verbose && t - last_stats > 5000) {
            last_stats = t;
            if (session_on || opt_loop)
                log_stats("stats");
        }
    }
    if (session_on)
        session_stop();
    logmsg("telaius-relay exit");
    return 0;
}
