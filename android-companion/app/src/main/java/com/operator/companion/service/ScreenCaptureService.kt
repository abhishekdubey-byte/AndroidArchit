package com.operator.companion.service

import android.app.Activity
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.content.res.Configuration
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import android.view.Surface
import androidx.core.app.ServiceCompat
import com.operator.companion.network.CompanionWebSocketClient
import com.operator.companion.relay.RemoteControl
import org.json.JSONObject

/**
 * The phone's screen as hardware H.264 for dashboards that reach the phone over Wi-Fi / the relay (roadmap Phase 5).
 *
 * MediaProjection (the user taps "Start now" once per session; the token lives only as long as this service) →
 * VirtualDisplay → MediaCodec input Surface, long side ≤ 1280, ≤ 30 fps, CBR 1.5–3 Mbps steered by the uplink queue
 * ([BitrateController]), key frame every 2 s and the previous frame repeated after 100 ms so a still screen keeps
 * late viewers fed. Packets go out as binary SCREEN_PKT frames (kind 1 config, 2 key, 3 delta) and the laptop puts
 * them on the same stream, fan-out and player as the USB mirror. Rotation re-creates the encoder on the same
 * VirtualDisplay (resize + new surface; Android 14 forbids a second display per token). Secure windows stay black.
 *
 * When nobody watches (dashboard tab hidden, viewer left) the laptop asks for a pause: the encoder stops and the
 * display renders nowhere, but the consent token is kept for [IDLE_STOP_MS], so mirroring resumes without asking
 * the user again. A "Start now" tapped after the laptop stopped waiting starts paused for the same reason.
 */
class ScreenCaptureService : Service() {
    companion object {
        private const val TAG = "ScreenCapture"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        const val ACTION_STOP = "stop"
        const val ACTION_KEYFRAME = "keyframe"
        const val ACTION_PAUSE = "pause"
        private const val IDLE_STOP_MS = 5 * 60_000L

        @Volatile var running = false
            private set
        /** The laptop currently wants pictures (false after a STOP/pause; set by START). */
        @Volatile var wanted = false

        fun pause(ctx: Context) {
            wanted = false
            if (running) runCatching { ctx.startService(Intent(ctx, ScreenCaptureService::class.java).setAction(ACTION_PAUSE)) }
        }

        fun stop(ctx: Context) {
            wanted = false
            runCatching { ctx.startService(Intent(ctx, ScreenCaptureService::class.java).setAction(ACTION_STOP)) }
        }

        /** Already sharing: resume a paused stream, or send a fresh key frame to a running one. */
        fun requestKeyFrame(ctx: Context) {
            wanted = true
            if (running) runCatching { ctx.startService(Intent(ctx, ScreenCaptureService::class.java).setAction(ACTION_KEYFRAME)) }
        }

        fun notify(active: Boolean, error: String? = null, extra: JSONObject? = null) {
            val c = CompanionWebSocketClient.activeInstance ?: return
            val p = JSONObject().put("active", active)
            if (error != null) p.put("error", error)
            extra?.keys()?.forEach { k -> p.put(k, extra.get(k)) }
            c.sendDirectJson(JSONObject().put("type", "SCREEN_STATE").put("device_id", c.deviceId).put("payload", p))
        }
    }

    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null
    @Volatile private var codec: MediaCodec? = null
    private var surface: Surface? = null
    private var drainThread: Thread? = null
    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var bitrate = BitrateController(start = 2_500_000, min = 600_000, max = 3_000_000)
    private var bitrateSized = false
    @Volatile private var skipping = false
    @Volatile private var waitKey = false
    private var lastTickMs = 0L
    private var lastThermalMs = 0L
    private var throttled = false
    private var size = 0 to 0
    private var stoppedByError = false
    private var paused = false
    private val idleStop = Runnable { Log.i(TAG, "idle for 5 min: ending screen sharing"); stopSelf() }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> { stopSelf(); return START_NOT_STICKY }
            ACTION_KEYFRAME -> {
                val h = handler
                if (h != null) h.post { if (paused) { h.removeCallbacks(idleStop); startEncoding() } else requestSync() }
                return START_NOT_STICKY
            }
            ACTION_PAUSE -> { handler?.post { pauseEncoding() }; return START_NOT_STICKY }
        }
        if (projection != null) { requestSync(); return START_NOT_STICKY }   // already live: just refresh viewers
        val code = intent?.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED) ?: Activity.RESULT_CANCELED
        @Suppress("DEPRECATION")
        val data: Intent? = intent?.getParcelableExtra(EXTRA_RESULT_DATA)
        if (code != Activity.RESULT_OK || data == null) { fail("Screen sharing was not allowed on the phone"); return START_NOT_STICKY }
        // Android 10+: the mediaProjection foreground service must run before the token is used
        val notif = com.operator.companion.relay.quietAppNotification(this)
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION else 0
        if (runCatching { ServiceCompat.startForeground(this, com.operator.companion.relay.RelayService.NOTIF_ID, notif, type) }.isFailure) {
            fail("The phone refused to start screen sharing"); return START_NOT_STICKY
        }
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val mp = runCatching { mpm.getMediaProjection(code, data) }.getOrNull()
            ?: run { fail("Screen sharing token was refused"); return START_NOT_STICKY }
        projection = mp
        thread = HandlerThread("ScreenCaptureThread").apply { start() }
        handler = Handler(thread!!.looper)
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {                     // user tapped "Stop sharing" or another app took over
                Log.i(TAG, "projection stopped by the system")
                stopSelf()
            }
        }, handler)
        handler!!.post { startEncoding() }
        running = true
        return START_NOT_STICKY
    }

    private fun displaySize(): Pair<Int, Int> {
        val d = RemoteControl.display(this)
        return d.getInt("width") to d.getInt("height")
    }

    /** Nobody watching: stop encoding (no heat, no traffic) but keep the token for a quick resume. */
    private fun pauseEncoding() {
        if (paused) return
        paused = true
        stopEncoder()
        runCatching { display?.surface = null }
        handler?.postDelayed(idleStop, IDLE_STOP_MS)
        notify(true, extra = JSONObject().put("paused", true))
        Log.i(TAG, "screen stream paused (token kept)")
    }

    /** (Re)create the encoder for the current orientation and point the virtual display at it. */
    private fun startEncoding() {
        val mp = projection ?: return
        if (!wanted) {                                  // consent arrived after the laptop stopped waiting
            if (display == null) {
                val (dw, dh) = displaySize()
                val (w, h) = ScreenSize.choose(dw, dh)
                display = mp.createVirtualDisplay("CompanionScreen", w, h, resources.displayMetrics.densityDpi,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, null, null, handler)
                size = w to h
            }
            pauseEncoding()
            return
        }
        paused = false
        try {
            val (dw, dh) = displaySize()
            val (w, h) = ScreenSize.choose(dw, dh)
            size = w to h
            stopEncoder()
            if (!bitrateSized) {                           // first start: bitrate to suit this screen size
                bitrateSized = true
                bitrate = BitrateController(start = ScreenSize.bitrate(w, h), min = 600_000, max = 3_000_000)
            }
            val enc = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
            val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h).apply {
                setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
                setInteger(MediaFormat.KEY_BIT_RATE, bitrate.current)
                setInteger(MediaFormat.KEY_FRAME_RATE, 30)
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
                setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 100_000)   // still screen: keep frames coming
                // 30 fps cap: official from Android 10; the same key is honoured by many older encoders too (scrcpy
                // sets it on every version) - measured 46 fps uncapped on an Android 9 phone
                setFloat("max-fps-to-encoder", 30f)
                val caps = enc.codecInfo.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC).encoderCapabilities
                if (caps.isBitrateModeSupported(MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR))
                    setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)
                setInteger(MediaFormat.KEY_PRIORITY, 0)
            }
            enc.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            val s = enc.createInputSurface()
            enc.start()
            codec = enc; surface = s
            skipping = false; waitKey = false
            val dpi = resources.displayMetrics.densityDpi
            val vd = display
            if (vd == null) {
                display = mp.createVirtualDisplay("CompanionScreen", w, h, dpi,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, s, null, handler)
            } else {
                vd.resize(w, h, dpi)
                vd.surface = s
            }
            drainThread = Thread({ drain(enc) }, "ScreenCaptureDrain").apply { start() }
            notify(true, extra = JSONObject().put("codec", "h264").put("width", w).put("height", h)
                .put("display_width", dw).put("display_height", dh).put("bitrate", bitrate.current))
            Log.i(TAG, "screen stream ${w}x$h (display ${dw}x$dh)")
        } catch (e: Exception) {
            Log.e(TAG, "encoder failed", e)
            fail("Screen encoder failed: ${e.message}")
        }
    }

    private fun stopEncoder() {
        val enc = codec
        codec = null
        try { drainThread?.join(500) } catch (_: Exception) {}
        drainThread = null
        try { enc?.stop() } catch (_: Exception) {}
        try { enc?.release() } catch (_: Exception) {}
        try { surface?.release() } catch (_: Exception) {}
        surface = null
    }

    private fun requestSync() {
        runCatching { codec?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) }) }
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        val h = handler ?: return
        h.post {
            val (dw, dh) = displaySize()
            if (!paused && ScreenSize.choose(dw, dh) != size) startEncoding()   // rotated: new size, new config packet
        }
    }

    private fun drain(enc: MediaCodec) {
        val info = MediaCodec.BufferInfo()
        while (codec === enc) {
            val idx = try { enc.dequeueOutputBuffer(info, 50_000) } catch (e: Exception) { break }
            if (idx >= 0) {
                try {
                    val buf = enc.getOutputBuffer(idx)
                    if (buf != null && info.size > 0) {
                        val data = ByteArray(info.size)
                        buf.position(info.offset); buf.limit(info.offset + info.size); buf.get(data)
                        val kind = when {
                            info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0 -> 1
                            info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0 -> 2
                            else -> 3
                        }
                        val send = kind == 1 || (!skipping && !(waitKey && kind == 3))
                        if (send) {
                            if (kind == 2) waitKey = false
                            CompanionWebSocketClient.activeInstance?.sendScreenPacket(kind, info.presentationTimeUs, data)
                        }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "packet failed: ${e.message}")
                } finally {
                    runCatching { enc.releaseOutputBuffer(idx, false) }
                }
            }
            tick(enc)
        }
    }

    private fun tick(enc: MediaCodec) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastTickMs < 1000) return
        lastTickMs = now
        val q = CompanionWebSocketClient.activeInstance?.queueBytes() ?: 0L
        bitrate.onQueue(q, now)?.let { bps ->
            runCatching { enc.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, bps) }) }
        }
        val skip = bitrate.updateSkip(q)
        if (skip && !skipping) skipping = true
        else if (!skip && skipping) { skipping = false; waitKey = true; requestSync() }
        if (now - lastThermalMs >= 10_000) {
            lastThermalMs = now
            val hot = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                (getSystemService(Context.POWER_SERVICE) as PowerManager).currentThermalStatus >= PowerManager.THERMAL_STATUS_MODERATE
            else ThermalZones.read() >= 45.0
            if (hot != throttled) {
                throttled = hot
                val bps = bitrate.capAt(if (hot) 1_200_000 else null)
                runCatching { enc.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, bps) }) }
            }
        }
    }

    private fun fail(msg: String) {
        stoppedByError = true
        notify(false, msg)
        stopSelf()
    }

    override fun onDestroy() {
        super.onDestroy()
        running = false
        val h = handler
        val done = java.util.concurrent.CountDownLatch(1)
        val cleanup = {
            h?.removeCallbacks(idleStop)
            stopEncoder()
            runCatching { display?.release() }
            display = null
            runCatching { projection?.stop() }
            projection = null
            done.countDown()
        }
        if (h != null) { h.post(cleanup); runCatching { done.await(1, java.util.concurrent.TimeUnit.SECONDS) } } else cleanup()
        thread?.quitSafely()
        thread = null; handler = null
        if (!stoppedByError) notify(false)
        runCatching { ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_DETACH) }
        Log.i(TAG, "screen stream stopped")
    }
}
