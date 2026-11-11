package com.operator.companion.service

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import android.util.Range
import android.util.Size
import android.view.Surface
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * Live camera as hardware H.264 (roadmap Phase 3).
 *
 * Camera2 renders straight into the encoder's input Surface (no CPU copies, no per-pixel rotation, no JPEG);
 * a drain thread hands each encoded packet to [onPacket] as kind 1 = codec config (SPS/PPS), 2 = key frame,
 * 3 = delta. Rotation/mirroring are reported via [onState] and applied by the viewer.
 * Once a second the uplink queue drives the bitrate ([BitrateController]); every 10 s a thermal check can drop
 * the camera to 15 fps and cap the bitrate until the phone has cooled for a minute.
 */
class CameraH264Streamer(
    private val context: Context,
    private val onPacket: (kind: Int, ptsUs: Long, data: ByteArray) -> Unit,
    private val onState: (JSONObject) -> Unit,
    private val onError: (String) -> Unit,
    private val queueBytes: () -> Long,
) {
    private val TAG = "CameraH264"
    private val cameraManager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager

    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    @Volatile private var running = false
    @Volatile private var lens = "back"

    private var cameraDevice: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    @Volatile private var codec: MediaCodec? = null
    private var inputSurface: Surface? = null
    private var drainThread: Thread? = null
    private var requestBuilder: CaptureRequest.Builder? = null
    private var normalFps: Range<Int>? = null
    private var slowFps: Range<Int>? = null

    private val bitrate = BitrateController()
    @Volatile private var skippingDeltas = false
    @Volatile private var waitKey = false          // after a skip: resume only at a key frame
    @Volatile private var generation = 0          // bumps on every close: callbacks of an older camera are ignored
    private var throttled = false
    private var coolSinceMs = 0L
    private var lastTickMs = 0L
    private var lastThermalMs = 0L
    // Camera timestamps are CLOCK_BOOTTIME on some phones and CLOCK_MONOTONIC on others; the microphone uses
    // MONOTONIC (System.nanoTime). Shift the video onto that clock once per encoder so audio and video line up.
    private var ptsShiftUs: Long? = null

    @Synchronized
    fun start(which: String) {
        val next = if (which.lowercase() == "front") "front" else "back"
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            onError("Camera permission not granted on device"); return
        }
        if (running) {
            if (next != lens) { lens = next; handler?.post { closeCamera(); openCamera() } }
            return
        }
        lens = next
        running = true
        if (thread == null) {
            thread = HandlerThread("CameraH264Thread").apply { start() }
            handler = Handler(thread!!.looper)
        }
        handler!!.post { openCamera() }
    }

    @Synchronized
    fun switchLens(which: String) {
        if (!running) return
        val next = if (which.lowercase() == "front") "front" else "back"
        if (next == lens) return
        lens = next
        handler!!.post { closeCamera(); openCamera() }
    }

    /** A new viewer joined or the uplink recovered: ask the encoder for a key frame now. */
    fun requestKeyFrame() {
        runCatching { codec?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) }) }
    }

    @Synchronized
    fun stop() {
        running = false
        val h = handler
        val t = thread
        if (h != null) {
            h.post { closeCamera() }
            // camera2 delivers its close callbacks on this thread: end it only after they have run
            h.postDelayed({ t?.quitSafely() }, 500)
        } else {
            closeCamera()
        }
        thread = null; handler = null
    }

    // ---------------- camera + encoder (handler thread) ----------------
    private fun chooseCameraId(facing: Int): String? = try {
        cameraManager.cameraIdList.firstOrNull {
            cameraManager.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == facing
        } ?: cameraManager.cameraIdList.firstOrNull()
    } catch (e: Exception) { null }

    private fun chooseSize(sizes: Array<Size>): Size {
        val fits = sizes.filter { it.width <= 1280 && it.height <= 720 }
        return fits.filter { it.width * 9 == it.height * 16 }.maxByOrNull { it.width * it.height }
            ?: fits.maxByOrNull { it.width * it.height }
            ?: sizes.minByOrNull { it.width * it.height } ?: Size(1280, 720)
    }

    @SuppressLint("MissingPermission")
    private fun openCamera() {
        if (!running) return
        val myGen = ++generation
        val front = lens == "front"
        val id = chooseCameraId(if (front) CameraCharacteristics.LENS_FACING_FRONT else CameraCharacteristics.LENS_FACING_BACK)
            ?: run { onError("No $lens camera on this device"); return }
        try {
            val chars = cameraManager.getCameraCharacteristics(id)
            val rotation = chars.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 0
            val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            val size = chooseSize(map?.getOutputSizes(SurfaceTexture::class.java) ?: arrayOf())
            val ranges = chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES) ?: arrayOf()
            normalFps = ranges.firstOrNull { it.lower == 30 && it.upper == 30 }
                ?: ranges.filter { it.upper == 30 }.maxByOrNull { it.lower }
                ?: ranges.maxByOrNull { it.upper }
            slowFps = ranges.firstOrNull { it.lower == 15 && it.upper == 15 }
                ?: ranges.filter { it.upper in 10..20 }.maxByOrNull { it.upper }

            val enc = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
            val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, size.width, size.height).apply {
                setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
                setInteger(MediaFormat.KEY_BIT_RATE, bitrate.current)
                setInteger(MediaFormat.KEY_FRAME_RATE, 30)
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
                val caps = enc.codecInfo.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC).encoderCapabilities
                if (caps.isBitrateModeSupported(MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR))
                    setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) setInteger(MediaFormat.KEY_PRIORITY, 0)  // real-time
            }
            enc.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            val surface = enc.createInputSurface()
            enc.start()
            codec = enc
            inputSurface = surface
            skippingDeltas = false
            waitKey = false
            ptsShiftUs = null
            drainThread = Thread({ drain(enc) }, "CameraH264Drain").apply { start() }

            onState(JSONObject().put("codec", "h264").put("width", size.width).put("height", size.height)
                .put("rotation", rotation).put("mirror", front).put("lens", lens).put("fps", normalFps?.upper ?: 30)
                .put("bitrate", bitrate.current))

            cameraManager.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) {
                    // a lens flip may have closed this attempt already: never attach a stale camera
                    if (!running || myGen != generation) { camera.close(); return }
                    cameraDevice = camera
                    try {
                    @Suppress("DEPRECATION")
                    camera.createCaptureSession(listOf(surface), object : CameraCaptureSession.StateCallback() {
                        override fun onConfigured(s: CameraCaptureSession) {
                            if (!running || myGen != generation) { s.close(); return }
                            session = s
                            try {
                                val b = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                                    addTarget(surface)
                                    set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                                    set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
                                    (if (throttled) slowFps else normalFps)?.let { set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
                                }
                                requestBuilder = b
                                s.setRepeatingRequest(b.build(), null, handler)
                            } catch (e: Exception) { onError("Camera request failed: ${e.message}") }
                        }
                        override fun onConfigureFailed(s: CameraCaptureSession) {
                            if (myGen == generation) onError("Camera session failed to configure")
                        }
                    }, handler)
                    } catch (e: Exception) {
                        if (myGen == generation) onError("Camera session failed: ${e.message}")
                    }
                }
                override fun onDisconnected(camera: CameraDevice) {
                    camera.close()
                    if (myGen == generation) onError("Another app took the camera")
                }
                override fun onError(camera: CameraDevice, error: Int) {
                    camera.close()
                    if (myGen != generation) return
                    onError(when (error) {
                        ERROR_CAMERA_IN_USE -> "Camera is in use by another app"
                        ERROR_MAX_CAMERAS_IN_USE -> "Too many cameras in use"
                        ERROR_CAMERA_DISABLED -> "Camera is disabled by device policy"
                        else -> "Camera error $error"
                    })
                }
            }, handler)
        } catch (e: Exception) {
            closeCamera()
            onError("Could not start the H.264 camera: ${e.message}")
        }
    }

    private fun closeCamera() {
        generation++
        try { session?.stopRepeating() } catch (_: Exception) {}
        try { session?.close() } catch (_: Exception) {}
        try { cameraDevice?.close() } catch (_: Exception) {}
        val enc = codec
        codec = null                                   // tells the drain thread to finish
        try { drainThread?.join(500) } catch (_: Exception) {}
        try { enc?.stop() } catch (_: Exception) {}
        try { enc?.release() } catch (_: Exception) {}
        try { inputSurface?.release() } catch (_: Exception) {}
        session = null; cameraDevice = null; inputSurface = null; drainThread = null; requestBuilder = null
    }

    // ---------------- encoder output (drain thread) ----------------
    private fun drain(enc: MediaCodec) {
        val info = MediaCodec.BufferInfo()
        while (running && codec === enc) {
            val idx = try { enc.dequeueOutputBuffer(info, 50_000) } catch (e: Exception) { break }
            if (idx >= 0) {
                try {
                    val buf = enc.getOutputBuffer(idx)
                    if (buf != null && info.size > 0) {
                        val data = ByteArray(info.size)
                        buf.position(info.offset)
                        buf.limit(info.offset + info.size)
                        buf.get(data)
                        val kind = when {
                            info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0 -> 1
                            info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0 -> 2
                            else -> 3
                        }
                        val send = when {
                            kind == 1 -> true                         // config is tiny and always needed
                            skippingDeltas -> false                   // congested: let the uplink drain
                            waitKey && kind == 3 -> false             // resume only at a key frame
                            else -> true
                        }
                        if (kind != 1 && ptsShiftUs == null) ptsShiftUs = clockShift(info.presentationTimeUs)
                        if (send) {
                            if (kind == 2) waitKey = false
                            onPacket(kind, if (kind == 1) info.presentationTimeUs else info.presentationTimeUs + (ptsShiftUs ?: 0L), data)
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

    /** 0 if [ptsUs] is already on System.nanoTime's clock, else the BOOTTIME→MONOTONIC difference. */
    private fun clockShift(ptsUs: Long): Long {
        val mono = System.nanoTime() / 1000
        val boot = SystemClock.elapsedRealtimeNanos() / 1000
        val shift = if (kotlin.math.abs(ptsUs - boot) < kotlin.math.abs(ptsUs - mono)) mono - boot else 0L
        Log.i(TAG, "video clock: ${if (shift == 0L) "monotonic" else "boottime (shift ${shift} us)"}")
        return shift
    }

    private fun tick(enc: MediaCodec) {
        val now = SystemClock.elapsedRealtime()       // monotonic: a wall-clock change can't stall the ticks
        if (now - lastTickMs < 1000) return
        lastTickMs = now
        val q = queueBytes()
        bitrate.onQueue(q, now)?.let { setBitrate(enc, it) }
        val skip = bitrate.updateSkip(q)
        if (skip && !skippingDeltas) {
            skippingDeltas = true                     // over 1 s queued: stop sending until it drains
        } else if (!skip && skippingDeltas) {
            skippingDeltas = false
            waitKey = true                            // drained: one key frame, then normal deltas
            requestKeyFrame()
        }
        if (now - lastThermalMs >= 10_000) {
            lastThermalMs = now
            val hot = isHot()
            if (hot) coolSinceMs = 0L else if (coolSinceMs == 0L) coolSinceMs = now
            if (hot && !throttled) setThrottled(enc, true)
            else if (!hot && throttled && now - coolSinceMs >= 60_000) setThrottled(enc, false)
        }
    }

    private fun setBitrate(enc: MediaCodec, bps: Int) {
        runCatching { enc.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, bps) }) }
    }

    private fun setThrottled(enc: MediaCodec, on: Boolean) {
        throttled = on
        setBitrate(enc, bitrate.capAt(if (on) 1_500_000 else null))
        val range = if (on) slowFps else normalFps
        val h = handler ?: return
        h.post {
            val b = requestBuilder ?: return@post
            val s = session ?: return@post
            runCatching {
                range?.let { b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
                s.setRepeatingRequest(b.build(), null, h)
            }
            onState(JSONObject().put("codec", "h264").put("throttled", on).put("fps", range?.upper ?: 30)
                .put("bitrate", bitrate.current))
        }
        Log.i(TAG, if (on) "phone is hot: 15 fps, bitrate capped" else "phone cooled: back to 30 fps")
    }

    private fun isHot(): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
            return pm.currentThermalStatus >= PowerManager.THERMAL_STATUS_MODERATE
        }
        return ThermalZones.read() >= 45.0
    }
}
