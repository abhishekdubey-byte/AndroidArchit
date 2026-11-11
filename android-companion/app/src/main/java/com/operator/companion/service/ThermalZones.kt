package com.operator.companion.service

import java.io.File

/**
 * Phone skin/board temperature from /sys/class/thermal on Android 9 and older (no PowerManager thermal API).
 * Only board/skin sensors count: radio-amplifier (pa_therm), battery-limit and CPU-limit zones report raw or
 * odd units (pa_therm0 says "105" on an SDM636), which would make a cool phone look hot.
 */
object ThermalZones {
    private val BOARD = Regex("^(quiet_therm|xo_therm|skin.*|.*skin_therm.*|board.*therm.*)$")   // skin-side sensors; msm_therm sits next to the SoC

    fun skinTempC(zones: List<Pair<String, String>>): Double {
        var hottest = 0.0
        for ((type, rawText) in zones) {
            if (!BOARD.matches(type)) continue
            val raw = rawText.trim().toDoubleOrNull() ?: continue
            val c = when { raw > 1000 -> raw / 1000; raw > 200 -> raw / 10; else -> raw }   // °C, deci-°C or milli-°C
            if (c in 1.0..85.0 && c > hottest) hottest = c
        }
        return hottest
    }

    fun read(): Double = skinTempC(
        File("/sys/class/thermal").listFiles()?.mapNotNull { zone ->
            val type = runCatching { File(zone, "type").readText().trim() }.getOrNull() ?: return@mapNotNull null
            val temp = runCatching { File(zone, "temp").readText().trim() }.getOrNull() ?: return@mapNotNull null
            type to temp
        } ?: emptyList())
}
