package com.operator.companion.diagnostics

import android.content.Context
import android.os.Build
import android.util.Log
import com.operator.companion.network.CompanionWebSocketClient
import org.json.JSONObject
import java.io.PrintWriter
import java.io.StringWriter
import java.util.concurrent.ConcurrentLinkedQueue

object RemoteDiagnosticManager {
    private const val TAG = "RemoteDiagnostic"
    private const val MAX_OFFLINE_QUEUE_SIZE = 50

    private val offlineQueue = ConcurrentLinkedQueue<JSONObject>()
    private var isHandlerInstalled = false

    /**
     * Installs global uncaught exception handler to prevent native OS crash popups
     * and instead dispatch structured diagnostic telemetry to the laptop.
     */
    fun installSilentCrashHandler(context: Context) {
        if (isHandlerInstalled) return
        isHandlerInstalled = true

        val defaultHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            Log.e(TAG, "Uncaught exception intercepted in thread ${thread.name}: ${throwable.message}", throwable)
            
            try {
                reportError(
                    source = "app_runtime",
                    operation = "uncaught_exception_${thread.name}",
                    errorCode = "CRASH_UNCAUGHT_EXCEPTION",
                    description = throwable.message ?: "Unknown fatal runtime exception",
                    suggestion = "Check application logs and verify device compatibility for thread ${thread.name}",
                    throwable = throwable
                )
            } catch (e: Exception) {
                Log.e(TAG, "Failed to report uncaught exception", e)
            }

            // Sleep briefly to ensure WebSocket transport has flushed
            try {
                Thread.sleep(1200)
            } catch (e: InterruptedException) {
                // Ignore
            }

            // Silently terminate process without displaying "App has stopped" dialog
            android.os.Process.killProcess(android.os.Process.myPid())
            System.exit(10)
        }
    }

    /**
     * Reports an error or hardware exception directly to the laptop.
     */
    fun reportError(
        source: String,
        operation: String,
        errorCode: String,
        description: String,
        suggestion: String,
        throwable: Throwable? = null
    ) {
        val stackTrace = if (throwable != null) {
            val sw = StringWriter()
            val pw = PrintWriter(sw)
            throwable.printStackTrace(pw)
            sw.toString()
        } else ""

        val errorObj = JSONObject().apply {
            put("type", "DEVICE_ERROR")
            put("timestamp", System.currentTimeMillis())
            put("device_info", JSONObject().apply {
                put("brand", Build.BRAND)
                put("manufacturer", Build.MANUFACTURER)
                put("model", Build.MODEL)
                put("android_version", Build.VERSION.RELEASE)
                put("sdk_int", Build.VERSION.SDK_INT)
            })
            put("module", source)
            put("operation", operation)
            put("error_code", errorCode)
            put("description", description)
            put("suggested_resolution", suggestion)
            put("stack_trace", stackTrace)
        }

        val client = CompanionWebSocketClient.activeInstance
        if (client != null && client.isConnected()) {
            errorObj.put("device_id", client.deviceId)
            client.sendDirectJson(errorObj)
            Log.d(TAG, "Transmitted diagnostic error to laptop: [$errorCode] $description")
        } else {
            // Buffer locally until laptop connection is restored
            if (offlineQueue.size >= MAX_OFFLINE_QUEUE_SIZE) {
                offlineQueue.poll()
            }
            offlineQueue.offer(errorObj)
            Log.w(TAG, "Buffered diagnostic error locally (offline queue: ${offlineQueue.size}): [$errorCode]")
        }
    }

    /**
     * Flushes buffered diagnostic messages upon WebSocket connection.
     */
    fun flushPendingDiagnostics(client: CompanionWebSocketClient) {
        var count = 0
        while (!offlineQueue.isEmpty()) {
            val item = offlineQueue.poll() ?: break
            item.put("device_id", client.deviceId)
            item.put("flushed_from_offline_queue", true)
            client.sendDirectJson(item)
            count++
        }
        if (count > 0) {
            Log.d(TAG, "Flushed $count pending diagnostic reports to laptop")
        }
    }
}
