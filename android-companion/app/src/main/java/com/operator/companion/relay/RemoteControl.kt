package com.operator.companion.relay

import android.Manifest
import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Rect
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.util.DisplayMetrics
import android.view.Display
import android.view.WindowManager
import android.view.accessibility.AccessibilityNodeInfo
import com.operator.companion.service.CompanionAccessibilityService
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Remote control requested by the laptop over the relay (same channel as file operations), so the
 * dashboard and the AI operator work when the phone is not on USB. Taps, keys, typing, screen
 * reading and screenshots use the app's accessibility service; apps/launch use the package manager.
 */
class RemoteControl(private val context: Context) {

    private fun svc(): CompanionAccessibilityService {
        if (com.operator.companion.BuildConfig.LITE) throw FsException(
            "This phone has Operator Companion Lite: remote screen view, taps, typing and app control need the " +
                "full version (install it over USB). Files, photos, videos and calls work.", 412)
        return CompanionAccessibilityService.instance
            ?: throw FsException("Remote screen view and control need the Operator Companion accessibility service. On the phone: " +
                "open Operator Companion → UNLOCK MORE POWERS → PILOT → Grant. If the switch is greyed out (Android 13+): " +
                "App info → ⋮ (top right) → \"Allow restricted settings\", then try again.", 412)
    }

    fun handle(op: String, args: JSONObject): FsResult = when (op) {
        "ctl.tap" -> gesture { cb -> svc().performTap(args.getDouble("x").toFloat(), args.getDouble("y").toFloat(), cb) }
        "ctl.swipe" -> gesture { cb ->
            svc().performSwipe(args.getDouble("x1").toFloat(), args.getDouble("y1").toFloat(),
                args.getDouble("x2").toFloat(), args.getDouble("y2").toFloat(), args.optLong("duration_ms", 300), cb)
        }
        "ctl.key" -> FsResult(key(args.getString("key")))
        "ctl.text" -> FsResult(text(args.getString("text")))
        "ctl.screenshot" -> FsResult(bytes = screenshot(args.optInt("width", 540), args.optInt("quality", 60)))
        "ctl.ui" -> FsResult(JSONObject().put("nodes", uiTree()))
        "ctl.apps" -> FsResult(JSONObject().put("apps", apps()))
        "ctl.launch" -> FsResult(launch(args.getString("package")))
        "ctl.call" -> FsResult(call(args.getString("number")))
        "ctl.display" -> FsResult(display(context))
        else -> throw FsException("Unknown control operation: $op")
    }

    private fun gesture(run: ((Boolean) -> Unit) -> Unit): FsResult {
        val latch = CountDownLatch(1)
        var ok = false
        run { done -> ok = done; latch.countDown() }
        if (!latch.await(5, TimeUnit.SECONDS)) throw FsException("Gesture timed out", 504)
        if (!ok) throw FsException("Android cancelled the gesture (screen locked or another gesture running)", 409)
        return FsResult(JSONObject().put("ok", true))
    }

    private fun key(name: String): JSONObject {
        val s = svc()
        val ok = when (name.uppercase()) {
            "BACK" -> s.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            "HOME" -> s.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME)
            "RECENTS", "APP_SWITCH" -> s.performGlobalAction(AccessibilityService.GLOBAL_ACTION_RECENTS)
            "NOTIFICATIONS" -> s.performGlobalAction(AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS)
            "POWER", "SLEEP" -> if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P)
                s.performGlobalAction(AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN)
                else throw FsException("Locking the screen remotely needs Android 9 or newer", 501)
            "WAKEUP" -> {
                @Suppress("DEPRECATION")
                val wl = (context.getSystemService(Context.POWER_SERVICE) as PowerManager).newWakeLock(
                    PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP, "acc:wake")
                wl.acquire(3000)
                true
            }
            "VOLUME_UP", "VOLUME_DOWN" -> {
                (context.getSystemService(Context.AUDIO_SERVICE) as AudioManager).adjustStreamVolume(
                    AudioManager.STREAM_MUSIC,
                    if (name.uppercase() == "VOLUME_UP") AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER,
                    AudioManager.FLAG_SHOW_UI)
                true
            }
            "ENTER" -> {
                val node = focusedInput() ?: throw FsException("No text field is focused on the phone", 409)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R)
                    node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)
                else throw FsException("Remote Enter needs Android 11 or newer", 501)
            }
            else -> throw FsException("Key $name is not supported remotely", 400)
        }
        return JSONObject().put("ok", ok)
    }

    @Suppress("DEPRECATION")
    private fun release(n: AccessibilityNodeInfo?) {
        if (n != null && Build.VERSION.SDK_INT < Build.VERSION_CODES.R) runCatching { n.recycle() }
    }

    private fun focusedInput(): AccessibilityNodeInfo? {
        val root = svc().rootInActiveWindow ?: return null
        root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.let { release(root); return it }
        fun find(n: AccessibilityNodeInfo): AccessibilityNodeInfo? {
            if (n.isEditable && n.isFocused) return n
            for (i in 0 until n.childCount) {
                val c = n.getChild(i) ?: continue
                val hit = find(c)
                if (hit != null) return hit
                release(c)
            }
            return null
        }
        val hit = find(root)
        if (hit !== root) release(root)
        return hit
    }

    private fun text(value: String): JSONObject {
        val node = focusedInput() ?: throw FsException("Tap a text field on the phone first", 409)
        val current = if (node.isShowingHintText) "" else node.text?.toString().orEmpty()
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, current + value)
        }
        return JSONObject().put("ok", node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args))
    }

    private fun screenshot(width: Int, quality: Int): ByteArray {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            throw FsException("Remote screen view needs Android 11 or newer on the phone (over USB it works on every version)", 501)
        }
        val latch = CountDownLatch(1)
        var bitmap: Bitmap? = null
        var error = -1
        svc().takeScreenshot(Display.DEFAULT_DISPLAY, shotExecutor,
            object : AccessibilityService.TakeScreenshotCallback {
                override fun onSuccess(result: AccessibilityService.ScreenshotResult) {
                    val hb = result.hardwareBuffer
                    val wrapped = Bitmap.wrapHardwareBuffer(hb, result.colorSpace)
                    bitmap = wrapped?.copy(Bitmap.Config.ARGB_8888, false)
                    wrapped?.recycle()
                    hb.close()
                    latch.countDown()
                }

                override fun onFailure(errorCode: Int) {
                    error = errorCode
                    latch.countDown()
                }
            })
        if (!latch.await(5, TimeUnit.SECONDS)) throw FsException("Screenshot timed out", 504)
        val full = bitmap ?: throw when (error) {
            AccessibilityService.ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT -> FsException("Screenshot requested too soon", 429)
            AccessibilityService.ERROR_TAKE_SCREENSHOT_SECURE_WINDOW -> FsException("This screen is protected (banking/DRM app); Android blocks capturing it", 403)
            else -> FsException("Android refused the screenshot (code $error)", 500)
        }
        val w = width.coerceIn(minOf(200, full.width), full.width)
        val scaled = if (w < full.width) Bitmap.createScaledBitmap(full, w, (full.height * w.toFloat() / full.width).toInt(), true) else full
        val out = ByteArrayOutputStream()
        scaled.compress(Bitmap.CompressFormat.JPEG, quality.coerceIn(30, 90), out)
        if (scaled !== full) scaled.recycle()
        full.recycle()
        return out.toByteArray()
    }

    /** Same shape as the laptop's UINode model (adb uiautomator dump). */
    private fun uiTree(): JSONArray {
        val root = svc().rootInActiveWindow ?: return JSONArray()
        var index = 0
        fun node(n: AccessibilityNodeInfo): JSONObject {
            val r = Rect()
            n.getBoundsInScreen(r)
            val children = JSONArray()
            for (i in 0 until n.childCount) n.getChild(i)?.let { children.put(node(it)); release(it) }
            return JSONObject()
                .put("index", index++)
                .put("text", n.text?.toString().orEmpty())
                .put("resource_id", n.viewIdResourceName.orEmpty())
                .put("class_name", n.className?.toString().orEmpty())
                .put("package", n.packageName?.toString().orEmpty())
                .put("content_desc", n.contentDescription?.toString().orEmpty())
                .put("clickable", n.isClickable)
                .put("selected", n.isSelected)   // e.g. a liked WhatsApp status ❤ (label stays "Heart reaction")
                .put("checked", n.isChecked)
                .put("bounds", JSONArray(listOf(r.left, r.top, r.right, r.bottom)))
                .put("children", children)
        }
        val tree = JSONArray().put(node(root))
        release(root)
        return tree
    }

    private fun apps(): JSONArray {
        val pm = context.packageManager
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val out = JSONArray()
        pm.queryIntentActivities(launcher, 0).map { it.activityInfo.applicationInfo }.distinctBy { it.packageName }
            .sortedBy { it.loadLabel(pm).toString().lowercase() }
            .forEach { ai ->
                out.put(JSONObject().put("package_name", ai.packageName).put("app_name", ai.loadLabel(pm).toString())
                    .put("is_system", ai.flags and ApplicationInfo.FLAG_SYSTEM != 0))
            }
        return out
    }

    private fun launch(pkg: String): JSONObject {
        val intent = context.packageManager.getLaunchIntentForPackage(pkg)
            ?: throw FsException("App $pkg is not installed or has no launcher screen", 404)
        svc() // starting another app from the background requires the accessibility service (Android 10+)
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        return JSONObject().put("ok", true)
    }

    private fun call(number: String): JSONObject {
        if (context.checkSelfPermission(Manifest.permission.CALL_PHONE) != PackageManager.PERMISSION_GRANTED) {
            throw FsException("Phone permission not granted to Operator Companion", 403)
        }
        svc()
        context.startActivity(Intent(Intent.ACTION_CALL, Uri.fromParts("tel", number, null)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        return JSONObject().put("ok", true)
    }

    companion object {
        // Screenshot callbacks + the heavy bitmap copy/compress run here, NOT on the main thread.
        // The app's accessibility service lives on the main thread and sits in the system input path,
        // so a ~10 MB full-screen bitmap copy there each frame stalls input and hangs the phone.
        // One process-wide thread (not per RemoteControl instance) so reconnects don't leak threads.
        private val shotExecutor = java.util.concurrent.Executors.newSingleThreadExecutor()

        fun display(context: Context): JSONObject {
            val m = DisplayMetrics()
            @Suppress("DEPRECATION")
            (context.getSystemService(Context.WINDOW_SERVICE) as WindowManager).defaultDisplay.getRealMetrics(m)
            return JSONObject().put("width", m.widthPixels).put("height", m.heightPixels).put("density", m.densityDpi)
        }
    }
}
