package com.operator.companion.service

/**
 * Adaptive bitrate for the live H.264 camera, driven by how much the uplink (OkHttp's send queue) is backed up.
 * Pure logic so it can be unit-tested on the JVM.
 *
 *  - queue > 512 KB  → step down ×0.7 (the link can't keep up)
 *  - queue < 64 KB for 5 s → step up ×1.15
 *  - queue > 2 MB    → the caller skips delta frames until the next key frame (OkHttp kills the socket at 16 MB)
 *  - capAt(bps)      → thermal cap; null removes it
 */
class BitrateController(
    start: Int = 2_500_000,
    private val min: Int = 800_000,
    private val max: Int = 4_000_000,
) {
    var current: Int = start.coerceIn(min, max)
        private set
    private var cap: Int? = null
    private var calmSince: Long? = 0L

    /** Feed the queue size once a second; returns the new bitrate when it changed, else null. */
    fun onQueue(queueBytes: Long, nowMs: Long): Int? {
        val ceiling = minOf(max, cap ?: max)
        return when {
            queueBytes > CONGESTED -> {
                calmSince = null
                change((current.toLong() * 70 / 100).toInt().coerceAtLeast(min))
            }
            queueBytes < CALM -> {
                val since = calmSince ?: nowMs.also { calmSince = it }
                if (nowMs - since >= CALM_MS) {
                    calmSince = nowMs
                    change((current.toLong() * 115 / 100).toInt().coerceAtMost(ceiling))
                } else null
            }
            else -> { calmSince = null; null }
        }
    }

    fun shouldSkipDeltas(queueBytes: Long): Boolean = queueBytes > SKIP_DELTAS

    /** Thermal cap (null = none). Returns the bitrate to use now. */
    fun capAt(maxBps: Int?): Int {
        cap = maxBps
        if (maxBps != null && current > maxBps) current = maxOf(min, maxBps)
        return current
    }

    private fun change(next: Int): Int? {
        if (next == current) return null
        current = next
        return next
    }

    companion object {
        const val CONGESTED = 512L * 1024
        const val CALM = 64L * 1024
        const val SKIP_DELTAS = 2L * 1000 * 1000
        const val CALM_MS = 5_000L
    }
}
