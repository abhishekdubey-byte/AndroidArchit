package com.operator.companion.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject

class CompanionAccessibilityService : AccessibilityService() {

    companion object {
        var instance: CompanionAccessibilityService? = null
            private set
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onDestroy() {
        super.onDestroy()
        if (instance == this) {
            instance = null
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Track active window or package changes
    }

    override fun onInterrupt() {}

    /**
     * Injects a tap gesture at the specified screen coordinates
     */
    fun performTap(x: Float, y: Float, callback: ((Boolean) -> Unit)? = null) {
        val path = Path().apply {
            moveTo(x, y)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, 50)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                callback?.invoke(true)
            }
            override fun onCancelled(gestureDescription: GestureDescription?) {
                callback?.invoke(false)
            }
        }, null)
    }

    /**
     * Injects a smooth swipe gesture between two coordinates
     */
    fun performSwipe(x1: Float, y1: Float, x2: Float, y2: Float, durationMs: Long = 300, callback: ((Boolean) -> Unit)? = null) {
        val path = Path().apply {
            moveTo(x1, y1)
            lineTo(x2, y2)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                callback?.invoke(true)
            }
            override fun onCancelled(gestureDescription: GestureDescription?) {
                callback?.invoke(false)
            }
        }, null)
    }

    /**
     * Dumps the current accessibility window tree into a JSON structure
     */
    fun dumpWindowHierarchy(): JSONObject {
        val rootNode = rootInActiveWindow ?: return JSONObject()
        return serializeNode(rootNode)
    }

    private fun serializeNode(node: AccessibilityNodeInfo): JSONObject {
        val obj = JSONObject()
        val rect = Rect()
        node.getBoundsInScreen(rect)

        obj.put("text", node.text?.toString() ?: "")
        obj.put("contentDescription", node.contentDescription?.toString() ?: "")
        obj.put("viewId", node.viewIdResourceName ?: "")
        obj.put("className", node.className?.toString() ?: "")
        obj.put("packageName", node.packageName?.toString() ?: "")
        obj.put("clickable", node.isClickable)
        obj.put("bounds", JSONArray(listOf(rect.left, rect.top, rect.right, rect.bottom)))

        val children = JSONArray()
        for (i in 0 until node.childCount) {
            val child = node.getChild(i)
            if (child != null) {
                children.put(serializeNode(child))
            }
        }
        obj.put("children", children)
        return obj
    }
}
