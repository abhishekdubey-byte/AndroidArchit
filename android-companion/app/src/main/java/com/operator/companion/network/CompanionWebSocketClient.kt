package com.operator.companion.network

import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.util.Log
import com.operator.companion.diagnostics.RemoteDiagnosticManager
import com.operator.companion.model.DeviceCapabilityDetector
import com.operator.companion.service.CallAndAudioBridgeService
import com.operator.companion.relay.FsException
import com.operator.companion.relay.RelayConfig
import com.operator.companion.relay.RemoteControl
import com.operator.companion.relay.RemoteFileManager
import com.operator.companion.service.CompanionAccessibilityService
import okhttp3.*
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.nio.ByteBuffer
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.*

class CompanionWebSocketClient(
    private val context: Context,
    private var serverUrls: List<String>,
    private var token: String,
    private val onStatusChange: (Boolean, String) -> Unit
) {
    private val TAG = "CompanionWS"
    @Volatile private var webSocket: WebSocket? = null
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .connectTimeout(8, TimeUnit.SECONDS)
        .pingInterval(20, TimeUnit.SECONDS) // detects dead mobile/NAT connections quickly
        .build()

    // Local-network addresses answer in milliseconds when reachable; fail fast so a phone on
    // mobile data reaches the internet relay quickly instead of waiting on unreachable LAN IPs.
    private val lanClient = client.newBuilder().connectTimeout(3, TimeUnit.SECONDS).build()
    private fun isLocal(url: String): Boolean {
        val host = runCatching { java.net.URI(url).host }.getOrNull() ?: return false
        return host.startsWith("10.") || host.startsWith("192.168.") ||
            Regex("^172\\.(1[6-9]|2\\d|3[01])\\.").containsMatchIn(host)
    }

    private var isUserDisconnected = false
    private var reconnectAttempt = 0
    private var reconnectJob: Job? = null
    private var urlIndex = 0
    private var openedOnCurrentUrl = false
    private val serverUrl: String get() = serverUrls[urlIndex % serverUrls.size]

    // Each connect() gets a new generation; callbacks from older sockets are ignored.
    private var generation = 0
    @Volatile private var acceptedByLaptop = false
    private var lastNetwork: Any? = null

    // Storage relay: quick ops and heavy ops (thumbnails, zips, copies) get separate pools
    // so a slow job never blocks browsing.
    private val fsExecutor = Executors.newFixedThreadPool(4)
    private val fsSlowExecutor = Executors.newFixedThreadPool(2)
    private val slowOps = setOf("thumb", "walk", "transfer", "delete", "ctl.screenshot", "ctl.ui", "ctl.apps")
    private val control by lazy { RemoteControl(context.applicationContext) }
    private val cancelled = ConcurrentHashMap.newKeySet<String>()
    @Volatile private var filesRef: RemoteFileManager? = null
    private val files: RemoteFileManager
        get() = filesRef ?: synchronized(this) { filesRef ?: RemoteFileManager(context.applicationContext).also { filesRef = it } }
    private var mediaReceiver: android.content.BroadcastReceiver? = null
    private val appUpdater by lazy { com.operator.companion.relay.AppUpdater(context.applicationContext, ::sendDirectJson) }

    private var callBridge: CallAndAudioBridgeService? = null

    /** Live camera preview runs in a transparent foreground activity (OEM background-camera policies
     *  block the sensor from a pure background service on ColorOS/Realme etc.). */
    private fun startCameraPreview(lens: String) {
try {
            // Flash a transient foreground activity so OEM camera policy allows the sensor, then it
            // starts the foreground service that actually holds the camera and survives app-switching.
            com.operator.companion.service.LivePreviewService.setLens(if (lens == "front") "front" else "back")
            val intent = android.content.Intent(context, com.operator.companion.LivePreviewLauncherActivity::class.java).apply {
                flags = android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_NO_ANIMATION
            }
            context.startActivity(intent)
        } catch (e: Exception) {
            Log.e(TAG, "Could not start live preview: ${e.message}")
            notifyCameraState(false, "Phone blocked opening the camera. Grant background/overlay access in setup.")
        }
    }

    private fun stopCameraPreview() {
        com.operator.companion.service.LivePreviewService.stop(context.applicationContext)
        notifyCameraState(false, null)
    }

    /** Called by LivePreviewService for each frame; drops frames if the uplink is congested. */
    fun sendCameraFrame(b64: String, lens: String, w: Int, h: Int) {
        val ws = webSocket ?: return
        if (ws.queueSize() < 2L * 1024 * 1024) {
            sendDirectJson(JSONObject().put("type", "CAMERA_FRAME").put("device_id", deviceId)
                .put("payload", JSONObject().put("data_base64", b64).put("lens", lens).put("width", w).put("height", h)))
        }
    }

    /** Mic audio that accompanies the live preview (PCM16 mono); dropped when the uplink is congested. */
    fun sendCameraAudio(b64Pcm: String, sampleRate: Int) {
        val ws = webSocket ?: return
        if (ws.queueSize() < 2L * 1024 * 1024) {
            sendDirectJson(JSONObject().put("type", "CAMERA_AUDIO").put("device_id", deviceId)
                .put("payload", JSONObject().put("pcm_base64", b64Pcm).put("sample_rate", sampleRate)))
        }
    }

    fun notifyCameraState(active: Boolean, error: String?, lens: String? = null) {
        val p = JSONObject().put("active", active)
        if (error != null) p.put("error", error)
        if (lens != null) p.put("lens", lens)
        sendDirectJson(JSONObject().put("type", "CAMERA_STREAM_STATE").put("device_id", deviceId).put("payload", p))
    }
    val deviceId: String by lazy { RelayConfig.deviceId(context) }

    companion object {
        @Volatile
        var activeInstance: CompanionWebSocketClient? = null
    }

    init {
        activeInstance = this
        RemoteDiagnosticManager.installSilentCrashHandler(context)

        // Initialize Telephony and Audio Bridge
        callBridge = CallAndAudioBridgeService(
            context = context,
            onTelephonyEvent = { eventType, phoneNumber ->
                sendTelephonyEvent(eventType, phoneNumber)
            },
            onAudioCaptured = { base64Pcm ->
                sendAudioChunk(base64Pcm)
            }
        )

        // Listen for HeadlessCameraActivity completion events
        val mediaFilter = android.content.IntentFilter().apply {
            addAction("com.operator.companion.PHOTO_COMPLETED")
            addAction("com.operator.companion.VIDEO_COMPLETED")
        }
        val receiver = object : android.content.BroadcastReceiver() {
            override fun onReceive(c: Context?, intent: android.content.Intent?) {
                val act = intent?.action ?: return
                if (act == "com.operator.companion.PHOTO_COMPLETED") {
                    val success = intent.getBooleanExtra("success", false)
                    val filePath = intent.getStringExtra("file_path") ?: ""
                    val error = intent.getStringExtra("error") ?: ""
                    val lens = intent.getStringExtra("lens") ?: "back"
                    val resp = JSONObject().apply {
                        put("type", "PHOTO_CAPTURED")
                        put("device_id", deviceId)
                        put("payload", JSONObject().apply {
                            put("success", success)
                            put("file_path", filePath)
                            put("error", error)
                            put("lens", lens)
                        })
                    }
                    sendDirectJson(resp)
                    Log.d(TAG, "Forwarded PHOTO_CAPTURED over WebSocket: $filePath")
                } else if (act == "com.operator.companion.VIDEO_COMPLETED") {
                    val success = intent.getBooleanExtra("success", false)
                    val filePath = intent.getStringExtra("file_path") ?: ""
                    val error = intent.getStringExtra("error") ?: ""
                    val duration = intent.getIntExtra("duration_sec", 5)
                    val lens = intent.getStringExtra("lens") ?: "back"
                    val resp = JSONObject().apply {
                        put("type", "VIDEO_RECORDED")
                        put("device_id", deviceId)
                        put("payload", JSONObject().apply {
                            put("success", success)
                            put("file_path", filePath)
                            put("parts", org.json.JSONArray((intent.getStringArrayExtra("parts") ?: emptyArray()).toList()))
                            put("error", error)
                            put("duration_sec", duration)
                            put("lens", lens)
                        })
                    }
                    sendDirectJson(resp)
                    Log.d(TAG, "Forwarded VIDEO_RECORDED over WebSocket: $filePath")
                }
            }
        }
        // Not exported: only this app's HeadlessCameraActivity may report captures.
        androidx.core.content.ContextCompat.registerReceiver(
            context, receiver, mediaFilter, androidx.core.content.ContextCompat.RECEIVER_NOT_EXPORTED)
        mediaReceiver = receiver
    }

    fun isConnected(): Boolean = webSocket != null

    @Synchronized
    fun connect() {
        isUserDisconnected = false
        reconnectJob?.cancel()
        val gen = ++generation
        webSocket?.let { webSocket = null; it.cancel() } // never leave an orphaned socket open

        if (serverUrls.isEmpty()) {
            onStatusChange(false, "Not paired with a laptop")
            return
        }
        val url = serverUrl
        openedOnCurrentUrl = false
        onStatusChange(false, "Connecting to ${hostOf(url)}…")
        val request = Request.Builder().url(url).header("X-ACC-Token", token).build()
        webSocket = (if (isLocal(url)) lanClient else client).newWebSocket(request, object : WebSocketListener() {
            private fun isCurrent(ws: WebSocket) = gen == generation

            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (!isCurrent(webSocket)) return
                Log.d(TAG, "Connected to Central Laptop Server via $url")
                openedOnCurrentUrl = true
                acceptedByLaptop = false
                onStatusChange(true, "Connected via ${hostOf(url)}")
                sendDeviceHello()
                sendCapabilities()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (!isCurrent(webSocket)) return
                if (!acceptedByLaptop) {
                    // The laptop answered (not just a relay accepting the socket): now the link is really up.
                    acceptedByLaptop = true
                    reconnectAttempt = 0
                    RemoteDiagnosticManager.flushPendingDiagnostics(this@CompanionWebSocketClient)
                    runCatching { appUpdater.check() }  // auto-update: ask the laptop for a newer version
                    runCatching { sendDirectJson(JSONObject().put("type", "WAITLIST_GET").put("device_id", deviceId)) }
                    runCatching { com.operator.companion.pb.PbStore.onConnected(context) }
                }
                handleIncomingMessage(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (!isCurrent(webSocket)) return
                try {
                    val buf = bytes.asByteBuffer()
                    val headerLen = buf.short.toInt() and 0xFFFF
                    val header = ByteArray(headerLen).also { buf.get(it) }
                    val payload = ByteArray(buf.remaining()).also { buf.get(it) }
                    val msg = JSONObject(String(header, Charsets.UTF_8))
                    when (msg.optString("type")) {
                        "FS_REQ" -> dispatchFs(msg, payload)
                        "APP_CHUNK" -> appUpdater.onChunk(msg, payload)
                        "PB_FILE_CHUNK" -> com.operator.companion.pb.PbStore.onFileChunk(msg, payload)
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Bad binary frame: ${e.message}")
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                when (code) {
                    4401 -> onSocketGone(gen, "Laptop rejected the access token — pair again", retry = false, retryAfterSec = 300)
                    // Identity secret lost (reinstall): plugging into the laptop over USB re-issues it;
                    // keep retrying slowly so a "Reset" on the laptop also takes effect without opening the app.
                    4403 -> onSocketGone(gen, "Laptop knows this phone from an earlier install — plug it into the laptop over USB, or on the laptop open Pair phones → Reset", retry = false, retryAfterSec = 300)
                    else -> onSocketGone(gen, "Disconnected ($reason)")
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "Connection error via $url: ${t.message}")
                if (response?.code == 401 || response?.code == 403) {
                    // Credentials are wrong everywhere: stop hammering (free relay quota); re-pair or a network change retries.
                    onSocketGone(gen, "Laptop rejected the access token — pair again", retry = false)
                } else {
                    onSocketGone(gen, "Can't reach ${hostOf(url)}: ${t.localizedMessage}")
                }
            }
        })
    }

    // Synchronized with connect(): an immediate failure cannot run before the new socket is recorded.
    @Synchronized
    private fun onSocketGone(gen: Int, msg: String, retry: Boolean = true, retryAfterSec: Long = 0) {
        if (gen != generation) return
        webSocket = null
        cancelled.clear()
        runCatching { appUpdater.onDisconnected() }  // resume the update from saved bytes on reconnect
        runCatching { com.operator.companion.pb.PbStore.onDisconnected() }
        runCatching { com.operator.companion.service.LivePreviewService.stop(context.applicationContext) }  // release the camera if the link drops
        filesRef?.abortAll() // laptop can no longer finish these uploads
        onStatusChange(false, msg)
        reconnectJob?.cancel()
        if (retry) scheduleReconnect()
        else if (retryAfterSec > 0) reconnectJob = CoroutineScope(Dispatchers.IO).launch {
            delay(retryAfterSec * 1000L)
            if (!isUserDisconnected && webSocket == null) connect()
        }
    }

    private fun hostOf(url: String) = runCatching { java.net.URI(url).let { "${it.host}${if (it.port > 0) ":" + it.port else ""}" } }.getOrDefault(url)

    /**
     * Android switched the default network (Wi-Fi <-> mobile data, new Wi-Fi): the old socket is dead
     * or on the wrong path, so reconnect right away from the first address instead of waiting for pings.
     */
    @Synchronized
    fun onNetworkAvailable(network: Any) {
        val changed = lastNetwork != null && lastNetwork != network
        lastNetwork = network
        if (isUserDisconnected || (!changed && webSocket != null)) return
        reconnectAttempt = 0
        urlIndex = 0
        connect()
    }

    /** New pairing data from the laptop: switch over without recreating the client. */
    @Synchronized
    fun reconfigure(urls: List<String>, newToken: String) {
        serverUrls = urls
        token = newToken
        urlIndex = 0
        reconnectAttempt = 0
        connect()
    }

    @Synchronized
    private fun scheduleReconnect() {
        if (isUserDisconnected) return
        if (!openedOnCurrentUrl && serverUrls.size > 1) {
            // This address is unreachable from the current network: try the next one right away.
            urlIndex = (urlIndex + 1) % serverUrls.size
            if (urlIndex != 0) {
                reconnectJob?.cancel()
                reconnectJob = CoroutineScope(Dispatchers.IO).launch {
                    delay(500)
                    if (!isUserDisconnected && webSocket == null) connect()
                }
                return
            }
        }
        reconnectAttempt++
        val delaySec = minOf(30L, 2L * reconnectAttempt)
        Log.d(TAG, "Scheduling reconnect attempt #$reconnectAttempt in ${delaySec}s...")

        reconnectJob?.cancel()
        reconnectJob = CoroutineScope(Dispatchers.IO).launch {
            delay(delaySec * 1000L)
            if (!isUserDisconnected && webSocket == null) {
                Log.d(TAG, "Executing auto-reconnect to $serverUrl...")
                connect()
            }
        }
    }

    @Synchronized
    fun disconnect() {
        isUserDisconnected = true
        generation++
        reconnectJob?.cancel()
        callBridge?.stopAudioStreaming()
        mediaReceiver?.let { runCatching { context.unregisterReceiver(it) } }
        mediaReceiver = null
        fsExecutor.shutdownNow()
        fsSlowExecutor.shutdownNow()
        filesRef?.abortAll()
        webSocket?.close(1000, "User disconnected")
        webSocket = null
        if (activeInstance == this) {
            activeInstance = null
        }
    }

    // ---------- headless camera launch ----------
    /** Since Android 10 a background app may only open a screen with one of these exemptions. */
    private fun canOpenScreenFromBackground(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return true
        if (CompanionAccessibilityService.instance != null) return true
        if (android.provider.Settings.canDrawOverlays(context)) return true
        val info = android.app.ActivityManager.RunningAppProcessInfo()
        android.app.ActivityManager.getMyMemoryState(info)
        return info.importance == android.app.ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
    }

    private fun launchHeadlessCamera(intent: android.content.Intent, resultType: String, lens: String, duration: Int) {
        if (!canOpenScreenFromBackground()) {
            sendDirectJson(JSONObject().put("type", resultType).put("device_id", deviceId).put("payload", JSONObject()
                .put("success", false).put("lens", lens).put("duration_sec", duration)
                .put("error", "Android blocked opening the camera from the background. On the phone, open Operator Companion → " +
                    "UNLOCK MORE POWERS → STEALTH → Grant (\"Display over other apps\"), or connect it by USB.")))
            return
        }
        if (Build.VERSION.SDK_INT >= 35 && CompanionAccessibilityService.instance == null) {
            // Android 15+: the overlay permission only counts while one of our overlay windows is visible.
            val main = android.os.Handler(android.os.Looper.getMainLooper())
            main.post {
                val wm = context.getSystemService(android.view.WindowManager::class.java)
                val dot = android.view.View(context)
                val lp = android.view.WindowManager.LayoutParams(1, 1,
                    android.view.WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                    android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or android.view.WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
                    android.graphics.PixelFormat.TRANSLUCENT)
                val added = runCatching { wm.addView(dot, lp) }.isSuccess
                main.postDelayed({ runCatching { context.startActivity(intent) } }, 150)
                if (added) main.postDelayed({ runCatching { wm.removeView(dot) } }, 3000)
            }
            return
        }
        context.startActivity(intent)
    }

    // ---------- storage relay ----------
    private fun dispatchFs(msg: JSONObject, payload: ByteArray?) {
        val reqId = msg.optString("req_id")
        val op = msg.optString("op")
        val args = msg.optJSONObject("args") ?: JSONObject()
        (if (op in slowOps) fsSlowExecutor else fsExecutor).execute {
            if (cancelled.remove(reqId)) return@execute // laptop no longer needs it (seek, closed tab, timeout)
            try {
                val res = if (op.startsWith("ctl.")) control.handle(op, args) else files.handle(op, args, payload)
                if (cancelled.remove(reqId)) return@execute
                if (res.bytes != null) sendFsBinary(reqId, res.bytes)
                else sendDirectJson(JSONObject().put("type", "FS_RES").put("req_id", reqId).put("ok", true).put("data", res.data ?: JSONObject()))
            } catch (e: Throwable) {
                val status = (e as? FsException)?.status ?: if (e is SecurityException) 403 else 500
                sendDirectJson(JSONObject().put("type", "FS_RES").put("req_id", reqId).put("ok", false)
                    .put("status", status).put("error", e.message ?: e.javaClass.simpleName))
            }
        }
    }

    private fun sendFsBinary(reqId: String, data: ByteArray) {
        val ws = webSocket ?: return
        // Back-pressure: OkHttp fails the socket if more than 16 MB is queued.
        var waited = 0
        while (ws.queueSize() > 8L * 1024 * 1024) {
            if (waited >= 30_000 || cancelled.remove(reqId)) {
                // Uplink congested: drop the payload rather than overflow OkHttp's 16 MB queue (which kills the socket)
                sendDirectJson(JSONObject().put("type", "FS_RES").put("req_id", reqId).put("ok", false)
                    .put("status", 503).put("error", "Phone upload link congested, try again"))
                return
            }
            Thread.sleep(10)
            waited += 10
        }
        val header = JSONObject().put("type", "FS_RES").put("req_id", reqId).put("ok", true).toString().toByteArray(Charsets.UTF_8)
        val frame = ByteBuffer.allocate(2 + header.size + data.size)
        frame.putShort(header.size.toShort()).put(header).put(data)
        ws.send(frame.array().toByteString())
    }

    /** PowerBoosters upload chunk: [2-byte header length][JSON header][bytes], same framing as FS_RES. */
    fun sendPbBinary(header: JSONObject, data: ByteArray): Boolean {
        val ws = webSocket ?: return false
        if (ws.queueSize() > 4L * 1024 * 1024) return false
        val h = header.toString().toByteArray(Charsets.UTF_8)
        val frame = ByteBuffer.allocate(2 + h.size + data.size)
        frame.putShort(h.size.toShort()).put(h).put(data)
        return ws.send(frame.array().toByteString())
    }

    fun isLinkUp(): Boolean = webSocket != null && acceptedByLaptop

    fun joinWaitlist() {
        sendDirectJson(JSONObject().put("type", "WAITLIST_JOIN").put("device_id", deviceId))
    }

    fun sendDirectJson(json: JSONObject): Boolean {
        val ws = webSocket ?: return false
        return try {
            ws.send(json.toString())
        } catch (e: Exception) {
            Log.e(TAG, "Failed to send JSON over WebSocket: ${e.message}")
            false
        }
    }

    private fun sendDeviceHello() {
        val bm = context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val batteryLevel = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)

        val hello = JSONObject().apply {
            put("type", "DEVICE_HELLO")
            put("device_id", deviceId)
            put("payload", JSONObject().apply {
                put("model", Build.MODEL)
                put("brand", Build.BRAND)
                put("manufacturer", Build.MANUFACTURER)
                put("android_version", Build.VERSION.RELEASE)
                put("sdk_int", Build.VERSION.SDK_INT)
                put("battery_level", batteryLevel)
                val d = RemoteControl.display(context)
                put("display_width", d.getInt("width"))
                put("display_height", d.getInt("height"))
                put("display_density", d.getInt("density"))
                put("edition", if (com.operator.companion.BuildConfig.LITE) "lite" else "full")
                put("version_code", com.operator.companion.BuildConfig.VERSION_CODE)
                put("device_owner", com.operator.companion.relay.AppLifecycle.isDeviceOwner(context))
                RelayConfig.secret(context).takeIf { it.isNotEmpty() }?.let { put("relay_secret", it) }
            })
        }
        sendDirectJson(hello)
    }

    /** The user may just have granted something on the phone: let the laptop's Permissions page see it now. */
    fun refreshCapabilities() {
        if (webSocket != null) Thread { sendCapabilities() }.start()  // camera queries: keep them off the UI thread
    }

    private fun sendCapabilities() {
        try {
            val caps = DeviceCapabilityDetector.detectCapabilities(context)
            val msg = JSONObject().apply {
                put("type", "DEVICE_CAPABILITIES")
                put("device_id", deviceId)
                put("payload", caps)
            }
            sendDirectJson(msg)
            Log.d(TAG, "Dispatched comprehensive device capabilities to laptop")
        } catch (e: Exception) {
            Log.e(TAG, "Error detecting device capabilities: ${e.message}")
        }
    }

    private fun sendTelephonyEvent(eventType: String, phoneNumber: String) {
        try {
            val event = JSONObject().apply {
                put("type", eventType)
                put("device_id", deviceId)
                put("payload", JSONObject().apply {
                    put("phone_number", phoneNumber)
                    put("state", if (eventType == "INCOMING_CALL") "ringing" else if (eventType == "CALL_ACTIVE") "active" else "idle")
                })
            }
            sendDirectJson(event)
            Log.d(TAG, "Dispatched telephony event to server: $eventType ($phoneNumber)")
        } catch (e: Exception) {
            Log.e(TAG, "Error sending telephony event", e)
        }
    }

    private fun sendAudioChunk(base64Pcm: String) {
        try {
            val audioMsg = JSONObject().apply {
                put("type", "PHONE_AUDIO_CHUNK")
                put("device_id", deviceId)
                put("payload", JSONObject().apply {
                    put("pcm_base64", base64Pcm)
                })
            }
            sendDirectJson(audioMsg)
        } catch (e: Exception) {
            // Silently ignore drop on disconnect
        }
    }

    private fun handleIncomingMessage(jsonStr: String) {
        try {
            val msg = JSONObject(jsonStr)
            val type = msg.optString("type")
            val payload = msg.optJSONObject("payload") ?: JSONObject()

            val service = CompanionAccessibilityService.instance

            if (type.startsWith("PB_")) { com.operator.companion.pb.PbStore.onMessage(type, payload); return }
            when (type) {
                "FS_REQ" -> dispatchFs(msg, null)
                "STOP_VIDEO" -> com.operator.companion.service.BackgroundCameraService.requestStopRecording()
                "SWITCH_VIDEO_LENS" -> com.operator.companion.service.BackgroundCameraService.requestSwitchLens(
                    msg.optJSONObject("payload")?.optString("lens", "back") ?: "back")
                "FS_CANCEL" -> {
                    if (cancelled.size > 2000) cancelled.clear()
                    cancelled.add(msg.optString("req_id"))
                }
                "APP_UPDATE_INFO" -> runCatching { appUpdater.onInfo(payload) }
                "APP_UPDATE_CHECK_NOW" -> runCatching { appUpdater.check() }
                "APP_UPDATE_FORCE" -> runCatching {
                    com.operator.companion.relay.AppLifecycle.updateRequested = true
                    com.operator.companion.relay.AppLifecycle.report(context, "update", "accepted")
                    appUpdater.check()
                }
                "APP_UNINSTALL" -> runCatching { com.operator.companion.relay.AppLifecycle.uninstall(context.applicationContext) }
                "WAITLIST_COUNT" -> {
                    com.operator.companion.relay.Waitlist.count.value = payload.optInt("count", com.operator.companion.relay.Waitlist.count.value)
                    if (payload.has("joined")) com.operator.companion.relay.Waitlist.joined.value = payload.optBoolean("joined")
                }
                "RELAY_BOUND" -> {
                    val secret = payload.optString("secret")
                    if (secret.isNotEmpty() && RelayConfig.setSecret(context, secret)) {
                        // Confirm exactly the secret we stored (by hash), so the laptop never finalises another one.
                        val h = java.security.MessageDigest.getInstance("SHA-256").digest(secret.toByteArray())
                            .joinToString("") { "%02x".format(it) }
                        sendDirectJson(JSONObject().put("type", "RELAY_BOUND_ACK").put("device_id", deviceId)
                            .put("payload", JSONObject().put("h", h)))
                    }
                }

                "GET_CAPABILITIES" -> {
                    sendCapabilities()
                }

                // UI & Gestures
                "INPUT_TOUCH" -> {
                    val x = payload.optDouble("x", 0.0).toFloat()
                    val y = payload.optDouble("y", 0.0).toFloat()
                    service?.performTap(x, y)
                }
                "INPUT_SWIPE" -> {
                    val x1 = payload.optDouble("x1", 0.0).toFloat()
                    val y1 = payload.optDouble("y1", 0.0).toFloat()
                    val x2 = payload.optDouble("x2", 0.0).toFloat()
                    val y2 = payload.optDouble("y2", 0.0).toFloat()
                    val dur = payload.optLong("duration_ms", 300)
                    service?.performSwipe(x1, y1, x2, y2, dur)
                }
                "INPUT_KEY" -> {
                    val key = payload.optString("key")
                    when (key.uppercase()) {
                        "BACK" -> service?.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK)
                        "HOME" -> service?.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_HOME)
                        "RECENTS" -> service?.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_RECENTS)
                    }
                }

                // Call Controls
                "ACCEPT_CALL" -> {
                    Log.d(TAG, "Received ACCEPT_CALL from laptop")
                    callBridge?.answerCall()
                }
                "END_CALL" -> {
                    Log.d(TAG, "Received END_CALL from laptop")
                    callBridge?.endCall()
                }

                // Downlink Audio Playback (Laptop Mic -> Phone Call Output)
                "PLAY_AUDIO" -> {
                    val pcmBase64 = payload.optString("pcm_base64")
                    if (pcmBase64.isNotEmpty()) {
                        callBridge?.playLaptopAudio(pcmBase64)
                    }
                }

                // Headless Background Camera & Video Capture (Zero UI, Low-Power Sensor Access)
                "CAPTURE_PHOTO" -> {
                    val lens = payload.optString("lens", "back")
                    Log.d(TAG, "Launching HeadlessCameraActivity for photo capture (lens: $lens)...")
                    val intent = android.content.Intent(context, com.operator.companion.HeadlessCameraActivity::class.java).apply {
                        flags = android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_NO_ANIMATION
                        putExtra("action", "photo")
                        putExtra("lens", lens)
                    }
                    launchHeadlessCamera(intent, "PHOTO_CAPTURED", lens, 0)
                }

                "START_CAMERA_STREAM" -> startCameraPreview(payload.optString("lens", "back"))
                "SET_CAMERA_LENS" -> com.operator.companion.service.LivePreviewService.switchLens(payload.optString("lens", "back"))
                "STOP_CAMERA_STREAM" -> stopCameraPreview()

                "RECORD_VIDEO" -> {
                    val lens = payload.optString("lens", "back")
                    val duration = payload.optInt("duration_sec", 5)
                    Log.d(TAG, "Launching HeadlessCameraActivity for video recording ($duration s, lens: $lens)...")
                    val intent = android.content.Intent(context, com.operator.companion.HeadlessCameraActivity::class.java).apply {
                        flags = android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_NO_ANIMATION
                        putExtra("action", "video")
                        putExtra("lens", lens)
                        putExtra("duration", duration)
                    }
                    launchHeadlessCamera(intent, "VIDEO_RECORDED", lens, duration)
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error handling message", e)
            RemoteDiagnosticManager.reportError(
                source = "companion_websocket",
                operation = "handle_incoming_message",
                errorCode = "MSG_PARSE_ERROR",
                description = e.message ?: "Failed to parse incoming WebSocket message",
                suggestion = "Verify JSON structure transmitted by server hub.",
                throwable = e
            )
        }
    }
}
