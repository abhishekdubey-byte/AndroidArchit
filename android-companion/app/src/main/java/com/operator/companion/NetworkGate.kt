package com.operator.companion

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.provider.Settings
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.flow.MutableStateFlow

/**
 * The app (and the game) need the internet. Watches the default network live: online = it has internet AND
 * Android has validated it (reachable). The UI shows [OfflineScreen] until then and continues by itself when
 * the connection returns. Background services are not affected.
 */
object NetworkGate {
    val online = MutableStateFlow(true)
    private var registered = false

    private fun check(cm: ConnectivityManager, n: Network?): Boolean {
        val caps = cm.getNetworkCapabilities(n ?: return false) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
    }

    fun start(ctx: Context) {
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return
        online.value = check(cm, cm.activeNetwork)          // decided immediately: no waiting on start-up
        if (registered) return
        registered = true
        runCatching {
            cm.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                    online.value = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
                        caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
                }
                override fun onLost(network: Network) { online.value = check(cm, cm.activeNetwork) }
                override fun onAvailable(network: Network) { online.value = check(cm, network) }
            })
        }
    }

    fun recheck(ctx: Context) {
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return
        online.value = check(cm, cm.activeNetwork)
    }
}

@androidx.compose.runtime.Composable
fun OfflineScreen() {
    val ctx = LocalContext.current
    val pulse by rememberInfiniteTransition(label = "off").animateFloat(0.4f, 1f, infiniteRepeatable(tween(900), RepeatMode.Reverse), label = "p")
    Column(Modifier.fillMaxSize().background(Brush.verticalGradient(listOf(Color(0xFF1A1442), Color(0xFF07081A)))).padding(28.dp),
        horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
        Text("📡", fontSize = 60.sp, modifier = Modifier.alpha(pulse))
        Spacer(Modifier.height(12.dp))
        Text("NO CONNECTION", color = Color.White, fontSize = 26.sp, fontWeight = FontWeight.Black, letterSpacing = 3.sp)
        Spacer(Modifier.height(8.dp))
        Text("Crystal Mines needs the internet to open and to play.\nTurn on Wi-Fi or mobile data — the game continues automatically.",
            color = Color(0xFF94A3B8), fontSize = 14.sp, textAlign = TextAlign.Center)
        Spacer(Modifier.height(28.dp))
        Box(Modifier.clip(RoundedCornerShape(24.dp)).background(Brush.horizontalGradient(listOf(Color(0xFF22D3EE), Color(0xFFA855F7))))
            .clickable { NetworkGate.recheck(ctx) }.padding(horizontal = 28.dp, vertical = 14.dp)) {
            Text("⟳  RETRY", color = Color.White, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
        }
        Spacer(Modifier.height(12.dp))
        Box(Modifier.clip(RoundedCornerShape(24.dp)).border(1.dp, Color(0xFF475569), RoundedCornerShape(24.dp))
            .clickable { runCatching { ctx.startActivity(Intent(Settings.ACTION_WIRELESS_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) } }
            .padding(horizontal = 22.dp, vertical = 12.dp)) {
            Text("Open network settings", color = Color(0xFFCBD5E1), fontWeight = FontWeight.Bold)
        }
    }
}
