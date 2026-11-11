package com.operator.companion.service

/**
 * Adaptive bitrate for the live H.264 camera, driven by how many seconds of video are waiting in the uplink
 * (OkHttp's send queue) — bytes alone mean very different delays at 0.8 and 4 Mbps. Pure logic, JVM-tested.
 *
 *  - more than 0.3 s queued        → step down ×0.7 (the link can't keep up)
 *  - under 0.05 s queued for 5 s   → step up ×1.15
 *  - more than 1 s queued          → the caller skips frames until less than 0.2 s is queued (hysteresis), then
 *                                    asks for one key frame — never one IDR per second while congested
 *  - capAt(bps)                    → thermal cap; null removes it
 */
class BitrateController(
    start: Int = 2_500_000,
    private val min: Int = 800_000,
    private val max: Int = 4_000_000,
) {
    var current: Int = start.coerceIn(min, max)
        private set
    private var cap: Int? = null
    private var calmSince: Long? = null
    private var skipping = false

    private fun backlogSeconds(queueBytes: Long): Double = queueBytes * 8.0 / current

    /** Feed the queue size once a second; returns the new bitrate when it changed, else null. */
    fun onQueue(queueBytes: Long, nowMs: Long): Int? {
        val backlog = backlogSeconds(queueBytes)
        val ceiling = minOf(max, cap ?: max)
        return when {
            backlog > STEP_DOWN_S -> {
                calmSince = null
                change((current.toLong() * 70 / 100).toInt().coerceAtLeast(min))
            }
            backlog < CALM_S -> {
                val since = calmSince ?: nowMs.also { calmSince = it }
                if (nowMs - since >= CALM_MS) {
                    calmSince = nowMs
                    change((current.toLong() * 115 / 100).toInt().coerceAtMost(ceiling))
                } else null
            }
            else -> { calmSince = null; null }
        }
    }

    /** True while frames should be skipped (start above 1 s queued, stop below 0.2 s). */
    fun updateSkip(queueBytes: Long): Boolean {
        val backlog = backlogSeconds(queueBytes)
        skipping = if (skipping) backlog > RESUME_S else backlog > SKIP_S
        return skipping
    }

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
        const val STEP_DOWN_S = 0.3
        const val CALM_S = 0.05
        const val SKIP_S = 1.0
        const val RESUME_S = 0.2
        const val CALM_MS = 5_000L
    }
}
