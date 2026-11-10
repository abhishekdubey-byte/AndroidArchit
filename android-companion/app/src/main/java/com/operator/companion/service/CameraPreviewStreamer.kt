package com.operator.companion.service

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.*
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.media.ImageReader
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Base64
import android.util.Log
import android.util.Size
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream

/**
 * Live camera preview: streams small JPEG frames from a lens to the laptop over the companion
 * link, so the Commander can watch in real time (verification / recognition) instead of grabbing
 * single stills. One session at a time; switching lens reopens the camera on the other sensor.
 *
 * Reuses the same warm-up idea as BackgroundCameraService: a continuous repeating preview request
 * lets auto-exposure settle, so frames are not black on Vivo/MediaTek/Samsung HALs.
 */
class CameraPreviewStreamer(
    private val context: Context,
    private val onFrame: (b64Jpeg: String, lens: String, w: Int, h: Int) -> Unit,
    private val onError: (String) -> Unit
) {
    private val TAG = "CameraPreview"
    private val cameraManager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager

    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    @Volatile private var running = false
    @Volatile private var lens = "back"
    @Volatile private var sensorRotation = 0     // degrees the sensor is mounted at; frames are rotated by this
    @Volatile private var lastSentAt = 0L
    private val minFrameGapMs = 130L            // ~7–8 fps; enough for live verification, light on the relay
    private val targetW = 640
    private val jpegQuality = 55

    private var cameraDevice: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var reader: ImageReader? = null

    @Synchronized
    fun start(which: String) {
        val next = if (which.lowercase() == "front") "front" else "back"
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            onError("Camera permission not granted on device"); return
        }
        if (running) {                 // idempotent: a repeated start never double-opens the camera
            if (next != lens) { lens = next; handler?.post { closeCamera(); openCamera() } }  // honoured as a lens change
            return
        }
        lens = next
        running = true
        if (thread == null) {
            thread = HandlerThread("CameraPreviewThread").apply { start() }
            handler = Handler(thread!!.looper)
        }
        handler!!.post { openCamera() }
    }

    @Synchronized
    fun switchLens(which: String) {
        if (!running) return   // a lens change after Stop must not silently reopen the camera
        val next = if (which.lowercase() == "front") "front" else "back"
        if (next == lens) return
        lens = next
        handler!!.post { closeCamera(); openCamera() }
    }

    // Keep the camera2 session callback on the same thread as open/close and the frame listener,
    // so session/cameraDevice/reader are only ever touched from the handler thread (no data race).
    private val handlerExecutor = java.util.concurrent.Executor { r -> handler?.post(r) ?: r.run() }

    @Synchronized
    fun stop() {
        running = false
        handler?.post { closeCamera() }
        thread?.quitSafely()
        thread = null; handler = null
    }

    // ---- camera plumbing (all on the preview handler thread) ----
    private fun chooseCameraId(facing: Int): String? {
        return try {
            cameraManager.cameraIdList.firstOrNull {
                cameraManager.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == facing
            } ?: cameraManager.cameraIdList.firstOrNull()
        } catch (e: Exception) { null }
    }

    @SuppressLint("MissingPermission")
    private fun openCamera() {
        if (!running) return
        val facing = if (lens == "front") CameraCharacteristics.LENS_FACING_FRONT else CameraCharacteristics.LENS_FACING_BACK
        val id = chooseCameraId(facing) ?: run { onError("No $lens camera on this device"); return }
        try {
            val chars = cameraManager.getCameraCharacteristics(id)
            sensorRotation = chars.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 0  // upright the sideways sensor feed
            val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            val sizes = map?.getOutputSizes(ImageFormat.YUV_420_888) ?: arrayOf()
            val size = sizes.filter { it.width <= 960 && it.height <= 960 }.minByOrNull { kotlin.math.abs(it.width - targetW) }
                ?: sizes.minByOrNull { it.width * it.height } ?: Size(640, 480)

            val ir = ImageReader.newInstance(size.width, size.height, ImageFormat.YUV_420_888, 2)
            reader = ir
            ir.setOnImageAvailableListener({ r ->
                val image = r.acquireLatestImage() ?: return@setOnImageAvailableListener
                try {
                    val now = System.currentTimeMillis()
                    if (running && now - lastSentAt >= minFrameGapMs) {
                        lastSentAt = now
                        val jpeg = yuvToJpeg(image, jpegQuality)
                        if (jpeg != null) onFrame(Base64.encodeToString(jpeg, Base64.NO_WRAP), lens, image.width, image.height)
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "frame encode failed: ${e.message}")
                } finally {
                    image.close()
                }
            }, handler)

            cameraManager.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) {
                    if (!running) { camera.close(); return }
                    cameraDevice = camera
                    val cb = object : CameraCaptureSession.StateCallback() {
                        override fun onConfigured(s: CameraCaptureSession) {
                            if (!running) { s.close(); return }
                            session = s
                            try {
                                val req = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                                    addTarget(ir.surface)
                                    set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                                    set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON)
                                }
                                s.setRepeatingRequest(req.build(), null, handler)
                            } catch (e: Exception) { onError("Preview request failed: ${e.message}") }
                        }
                        override fun onConfigureFailed(s: CameraCaptureSession) { closeCamera(); onError("Camera preview session failed to configure") }
                    }
                    try {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                            camera.createCaptureSession(SessionConfiguration(
                                SessionConfiguration.SESSION_REGULAR, listOf(OutputConfiguration(ir.surface)),
                                handlerExecutor, cb))
                        } else {
                            @Suppress("DEPRECATION") camera.createCaptureSession(listOf(ir.surface), cb, handler)
                        }
                    } catch (e: Exception) { onError("Could not start preview: ${e.message}") }
                }
                override fun onDisconnected(camera: CameraDevice) { camera.close(); try { ir.close() } catch (_: Exception) {} }
                override fun onError(camera: CameraDevice, error: Int) {
                    camera.close()
                    try { ir.close() } catch (_: Exception) {}
                    onError(when (error) {
                        ERROR_CAMERA_IN_USE -> "Camera is in use by another app"
                        ERROR_MAX_CAMERAS_IN_USE -> "Too many cameras in use"
                        ERROR_CAMERA_DISABLED -> "Camera is disabled by device policy"
                        else -> "Camera error $error"
                    })
                }
            }, handler)
        } catch (e: Exception) {
            try { reader?.close() } catch (_: Exception) {}
            reader = null
            onError("Could not open camera: ${e.message}")
        }
    }

    private fun closeCamera() {
        try { session?.stopRepeating() } catch (_: Exception) {}
        try { session?.close() } catch (_: Exception) {}
        try { cameraDevice?.close() } catch (_: Exception) {}
        try { reader?.close() } catch (_: Exception) {}
        session = null; cameraDevice = null; reader = null
    }

    /** Rotate an NV21 buffer 90/180/270° clockwise; returns the rotated bytes and new width/height. */
    private fun rotateNv21(src: ByteArray, w: Int, h: Int, rotation: Int): Triple<ByteArray, Int, Int> {
        val out = ByteArray(src.size)
        val frame = w * h
        when (rotation) {
            90 -> {
                var i = 0
                for (x in 0 until w) for (y in h - 1 downTo 0) out[i++] = src[y * w + x]
                i = frame
                for (x in 0 until w step 2) for (y in h / 2 - 1 downTo 0) {
                    out[i++] = src[frame + y * w + x]; out[i++] = src[frame + y * w + x + 1]
                }
                return Triple(out, h, w)
            }
            270 -> {
                var i = 0
                for (x in w - 1 downTo 0) for (y in 0 until h) out[i++] = src[y * w + x]
                i = frame
                for (x in w - 2 downTo 0 step 2) for (y in 0 until h / 2) {
                    out[i++] = src[frame + y * w + x]; out[i++] = src[frame + y * w + x + 1]
                }
                return Triple(out, h, w)
            }
            180 -> {
                var i = 0
                for (y in h - 1 downTo 0) for (x in w - 1 downTo 0) out[i++] = src[y * w + x]
                i = frame
                for (y in h / 2 - 1 downTo 0) for (x in w - 2 downTo 0 step 2) {
                    out[i++] = src[frame + y * w + x]; out[i++] = src[frame + y * w + x + 1]
                }
                return Triple(out, w, h)
            }
            else -> return Triple(src, w, h)
        }
    }

    /** YUV_420_888 → NV21 → JPEG, honouring row/pixel strides (correct across OEM sensors). */
    private fun yuvToJpeg(image: android.media.Image, quality: Int): ByteArray? {
        val w = image.width; val h = image.height
        val y = image.planes[0]; val u = image.planes[1]; val v = image.planes[2]
        val nv21 = ByteArray(w * h * 3 / 2)
        // Y
        var pos = 0
        val yBuf = y.buffer; val yRow = y.rowStride; val yPix = y.pixelStride
        for (row in 0 until h) {
            var col = row * yRow
            if (yPix == 1) { yBuf.position(col); yBuf.get(nv21, pos, w); pos += w }
            else { for (c in 0 until w) { nv21[pos++] = yBuf.get(col); col += yPix } }
        }
        // VU interleaved for NV21
        val uBuf = u.buffer; val vBuf = v.buffer
        val uRow = u.rowStride; val vRow = v.rowStride; val uPix = u.pixelStride; val vPix = v.pixelStride
        val cw = w / 2; val ch = h / 2
        for (row in 0 until ch) {
            var uCol = row * uRow; var vCol = row * vRow
            for (c in 0 until cw) {
                nv21[pos++] = vBuf.get(vCol)
                nv21[pos++] = uBuf.get(uCol)
                uCol += uPix; vCol += vPix
            }
        }
        // Rotate the sensor frame upright before encoding, so both the laptop preview and the saved
        // recording are the right way up (phones mount the sensor at 90°/270°).
        var data = nv21; var ow = w; var oh = h
        val rot = ((sensorRotation % 360) + 360) % 360
        if (rot == 90 || rot == 180 || rot == 270) {
            val r = rotateNv21(nv21, w, h, rot); data = r.first; ow = r.second; oh = r.third
        }
        return try {
            val out = ByteArrayOutputStream()
            YuvImage(data, ImageFormat.NV21, ow, oh, null).compressToJpeg(Rect(0, 0, ow, oh), quality, out)
            out.toByteArray()
        } catch (e: Exception) { null }
    }
}
