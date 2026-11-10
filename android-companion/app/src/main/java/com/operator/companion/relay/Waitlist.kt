package com.operator.companion.relay

import kotlinx.coroutines.flow.MutableStateFlow

/** Shared waitlist counter, fed by the server over the companion connection. */
object Waitlist {
    val count = MutableStateFlow(370)   // base; the server replaces this with the real total
    val joined = MutableStateFlow(false)
}
