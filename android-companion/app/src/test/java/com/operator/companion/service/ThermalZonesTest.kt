package com.operator.companion.service

import org.junit.Assert.assertEquals
import org.junit.Test

class ThermalZonesTest {
    @Test fun ignoresRadioAmplifierAndLimitSensors() {
        // Redmi Note 5 Pro (SDM636) as read by the app: pa_therm0 reports a raw value, not °C
        val zones = listOf("pa_therm0" to "105", "msm_therm" to "43", "quiet_therm" to "42", "xo_therm" to "42",
                           "limits_sensor-00" to "x", "backlight_therm" to "33")
        assertEquals(43.0, ThermalZones.skinTempC(zones), 0.01)
    }

    @Test fun normalisesDeciAndMilliDegrees() {
        assertEquals(46.0, ThermalZones.skinTempC(listOf("quiet_therm" to "460")), 0.01)
        assertEquals(45.5, ThermalZones.skinTempC(listOf("skin-therm" to "45500")), 0.01)
    }

    @Test fun implausibleValuesIgnored() {
        assertEquals(0.0, ThermalZones.skinTempC(listOf("msm_therm" to "-40", "xo_therm" to "999999")), 0.01)
    }
}
