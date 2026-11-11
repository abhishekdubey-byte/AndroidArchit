package com.operator.companion.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BitrateControllerTest {
    @Test fun stepsDownWhenTheUplinkQueueGrows() {
        val c = BitrateController(start = 2_500_000)
        assertEquals(1_750_000, c.onQueue(600_000, nowMs = 1_000))
        assertEquals(1_225_000, c.onQueue(600_000, nowMs = 2_000))
    }

    @Test fun neverBelowMin() {
        val c = BitrateController(start = 1_000_000, min = 800_000)
        assertEquals(800_000, c.onQueue(900_000, nowMs = 1_000))
        assertNull(c.onQueue(900_000, nowMs = 2_000))      // already at the floor: no change
    }

    @Test fun stepsUpOnlyAfterFiveCalmSeconds() {
        val c = BitrateController(start = 1_000_000)
        assertNull(c.onQueue(10_000, nowMs = 1_000))
        assertNull(c.onQueue(10_000, nowMs = 4_000))
        assertEquals(1_150_000, c.onQueue(10_000, nowMs = 6_100))
        assertNull(c.onQueue(10_000, nowMs = 7_000))        // next step needs another 5 calm seconds
    }

    @Test fun congestionResetsTheCalmTimer() {
        val c = BitrateController(start = 1_000_000)
        c.onQueue(10_000, nowMs = 0)
        c.onQueue(300_000, nowMs = 3_000)                   // between thresholds: not calm
        assertNull(c.onQueue(10_000, nowMs = 6_000))
    }

    @Test fun neverAboveMaxOrThermalCap() {
        val c = BitrateController(start = 3_900_000, max = 4_000_000)
        assertEquals(4_000_000, c.onQueue(0, nowMs = 6_000))
        assertEquals(1_500_000, c.capAt(1_500_000))
        assertNull(c.onQueue(0, nowMs = 20_000))            // capped: cannot climb
        c.capAt(null)
        assertEquals(1_725_000, c.onQueue(0, nowMs = 30_000))
    }

    @Test fun skipDeltasOnlyWhenBadlyCongested() {
        val c = BitrateController()
        assertFalse(c.shouldSkipDeltas(1_000_000))
        assertTrue(c.shouldSkipDeltas(2_100_000))
    }
}
