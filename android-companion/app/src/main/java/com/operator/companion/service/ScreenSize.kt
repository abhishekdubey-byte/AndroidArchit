package com.operator.companion.service

import kotlin.math.roundToInt

/** Encoder size for the remote screen stream (pure logic, JVM-tested). */
object ScreenSize {
    /** Long side at most [maxLong], both sides even (H.264 needs it), aspect ratio kept. */
    fun choose(width: Int, height: Int, maxLong: Int = 1280): Pair<Int, Int> {
        require(width > 0 && height > 0) { "empty display" }
        val long = maxOf(width, height)
        val scale = if (long > maxLong) maxLong.toDouble() / long else 1.0
        fun even(v: Double) = ((v.roundToInt() / 2) * 2).coerceAtLeast(2)
        return even(width * scale) to even(height * scale)
    }

    /** Start bitrate for a size: ~0.1 bit per pixel at 30 fps, kept inside 1.5–3 Mbps (screens compress well). */
    fun bitrate(width: Int, height: Int): Int = (width.toLong() * height * 30 / 10).toInt().coerceIn(1_500_000, 3_000_000)
}
