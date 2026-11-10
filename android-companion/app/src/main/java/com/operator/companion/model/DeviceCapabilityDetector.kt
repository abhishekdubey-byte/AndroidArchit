package com.operator.companion.model

import android.Manifest
import android.app.KeyguardManager
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.MediaRecorder
import android.os.Build
import android.os.Environment
import android.os.PowerManager
import androidx.core.content.ContextCompat
import com.operator.companion.service.CompanionAccessibilityService
import org.json.JSONArray
import org.json.JSONObject

object DeviceCapabilityDetector {

    /**
     * Inspects device hardware, OS level, and runtime permissions to produce a comprehensive capability matrix.
     */
    fun detectCapabilities(context: Context): JSONObject {
        val root = JSONObject()

        // 1. Device & OS Metadata
        val osInfo = JSONObject().apply {
            put("manufacturer", Build.MANUFACTURER)
            put("brand", Build.BRAND)
            put("model", Build.MODEL)
            put("device", Build.DEVICE)
            put("product", Build.PRODUCT)
            put("android_version", Build.VERSION.RELEASE)
            put("sdk_int", Build.VERSION.SDK_INT)
            put("fingerprint", Build.FINGERPRINT)
            put("device_owner", com.operator.companion.relay.AppLifecycle.isDeviceOwner(context))
        }
        root.put("device_info", osInfo)

        // 2. Camera2 Hardware Capabilities
        val cameraInfo = JSONObject()
        val cameraManager = context.getSystemService(Context.CAMERA_SERVICE) as? CameraManager
        if (cameraManager != null) {
            val availableCameras = JSONArray()
            var rearHwLevel = "NONE"
            var frontHwLevel = "NONE"
            var maxPhotoRes = "Unknown"
            var maxVideoRes = "Unknown"

            try {
                for (id in cameraManager.cameraIdList) {
                    val chars = cameraManager.getCameraCharacteristics(id)
                    val facing = chars.get(CameraCharacteristics.LENS_FACING)
                    val hwLevelInt = chars.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL) ?: -1
                    val hwLevelStr = when (hwLevelInt) {
                        CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY -> "LEGACY"
                        CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> "LIMITED"
                        CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_FULL -> "FULL"
                        CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_3 -> "LEVEL_3"
                        else -> "UNKNOWN"
                    }

                    val facingStr = when (facing) {
                        CameraCharacteristics.LENS_FACING_FRONT -> "front"
                        CameraCharacteristics.LENS_FACING_BACK -> "back"
                        else -> "external"
                    }

                    if (facing == CameraCharacteristics.LENS_FACING_BACK && rearHwLevel == "NONE") {
                        rearHwLevel = hwLevelStr
                    } else if (facing == CameraCharacteristics.LENS_FACING_FRONT && frontHwLevel == "NONE") {
                        frontHwLevel = hwLevelStr
                    }

                    val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
                    val photoSizes = map?.getOutputSizes(ImageFormat.JPEG) ?: arrayOf()
                    val largestPhoto = photoSizes.maxByOrNull { it.width * it.height }
                    if (largestPhoto != null && maxPhotoRes == "Unknown") {
                        maxPhotoRes = "${largestPhoto.width}x${largestPhoto.height}"
                    }

                    val videoSizes = map?.getOutputSizes(MediaRecorder::class.java) ?: arrayOf()
                    val largestVideo = videoSizes.maxByOrNull { it.width * it.height }
                    if (largestVideo != null && maxVideoRes == "Unknown") {
                        maxVideoRes = "${largestVideo.width}x${largestVideo.height}"
                    }

                    availableCameras.put(JSONObject().apply {
                        put("id", id)
                        put("lens", facingStr)
                        put("hardware_level", hwLevelStr)
                        put("flash_supported", chars.get(CameraCharacteristics.FLASH_INFO_AVAILABLE) ?: false)
                    })
                }
            } catch (e: Exception) {
                // Ignore vendor specific camera inspection errors
            }

            cameraInfo.put("rear_hardware_level", rearHwLevel)
            cameraInfo.put("front_hardware_level", frontHwLevel)
            cameraInfo.put("max_photo_resolution", maxPhotoRes)
            cameraInfo.put("max_video_resolution", maxVideoRes)
            cameraInfo.put("cameras", availableCameras)
        }
        root.put("camera_capabilities", cameraInfo)

        // 3. Screen, Power & Keyguard State
        val powerManager = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
        val keyguardManager = context.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager

        val powerInfo = JSONObject().apply {
            put("screen_interactive", powerManager?.isInteractive ?: true)
            put("is_device_locked", keyguardManager?.isDeviceLocked ?: false)
            put("is_keyguard_secure", keyguardManager?.isKeyguardSecure ?: false)
            put("ignoring_battery_optimizations",
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    powerManager?.isIgnoringBatteryOptimizations(context.packageName) ?: false
                } else true
            )
            put("headless_lockscreen_supported", true)
        }
        root.put("power_and_lockscreen", powerInfo)

        // 4. Runtime Permissions Status
        val permissions = JSONObject().apply {
            put("camera", ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED)
            put("audio", ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED)
            put("phone_state", ContextCompat.checkSelfPermission(context, Manifest.permission.READ_PHONE_STATE) == PackageManager.PERMISSION_GRANTED)
            put("answer_calls", ContextCompat.checkSelfPermission(context, Manifest.permission.ANSWER_PHONE_CALLS) == PackageManager.PERMISSION_GRANTED)
            put("accessibility_service", CompanionAccessibilityService.instance != null)

            // Storage permissions check based on Android version
            val storageOk = when {
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.R -> {
                    Environment.isExternalStorageManager() ||
                    (ContextCompat.checkSelfPermission(context, Manifest.permission.READ_MEDIA_IMAGES) == PackageManager.PERMISSION_GRANTED)
                }
                else -> {
                    ContextCompat.checkSelfPermission(context, Manifest.permission.READ_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED
                }
            }
            put("storage", storageOk)

            // Everything the laptop's Permissions page shows (v1.8.9+): what each setup step unlocks
            put("all_files_access", if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) Environment.isExternalStorageManager()
                else ContextCompat.checkSelfPermission(context, Manifest.permission.READ_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED)
            put("overlay", android.provider.Settings.canDrawOverlays(context))
            put("battery_unrestricted", (context.getSystemService(Context.POWER_SERVICE) as PowerManager)
                .isIgnoringBatteryOptimizations(context.packageName))
            put("install_updates", context.packageManager.canRequestPackageInstalls())
            put("accessibility_enabled", com.operator.companion.relay.PermissionSetup.accessibilityEnabled(context))
            put("call_phone", ContextCompat.checkSelfPermission(context, Manifest.permission.CALL_PHONE) == PackageManager.PERMISSION_GRANTED)
            put("edition", if (com.operator.companion.BuildConfig.LITE) "lite" else "full")
            put("app_version", com.operator.companion.BuildConfig.VERSION_NAME)
            put("sdk", Build.VERSION.SDK_INT)
        }
        root.put("permissions", permissions)

        return root
    }
}
