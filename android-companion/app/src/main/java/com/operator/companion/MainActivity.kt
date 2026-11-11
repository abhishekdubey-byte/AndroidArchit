package com.operator.companion

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import android.net.Uri
import android.os.PowerManager
import android.widget.Toast
import androidx.compose.ui.text.input.PasswordVisualTransformation
import com.operator.companion.relay.RelayConfig
import com.operator.companion.relay.RelayService
import com.operator.companion.relay.RelayStatus
import com.operator.companion.relay.RemoteFileManager
import com.operator.companion.relay.OemBackground
import com.operator.companion.relay.PermissionSetup
import com.operator.companion.service.CompanionAccessibilityService

class MainActivity : ComponentActivity() {

    private val resumeTick = mutableIntStateOf(0)
    private val setupSkipped = mutableStateOf(false)

    companion object {
        fun getRequiredPermissions(): Array<String> {
            val list = mutableListOf(
                Manifest.permission.CAMERA,
                Manifest.permission.RECORD_AUDIO,
                Manifest.permission.READ_PHONE_STATE,
                Manifest.permission.ANSWER_PHONE_CALLS
            )
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                list.add(Manifest.permission.READ_MEDIA_IMAGES)
                list.add(Manifest.permission.READ_MEDIA_VIDEO)
            } else {
                list.add(Manifest.permission.READ_EXTERNAL_STORAGE)
            }
            return list.toTypedArray()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        com.operator.companion.diagnostics.RemoteDiagnosticManager.installSilentCrashHandler(applicationContext)

        RelayConfig.ensureDefaults(this) // connect to the built-in laptop server, no typing needed
        setupSkipped.value = getSharedPreferences("setup", Context.MODE_PRIVATE).getBoolean("skipped", false)
        startIfReady() // bring the link up now; feature permissions are granted later on the setup screen
        if (savedInstanceState == null) handlePairing(intent)
        if (intent?.getBooleanExtra("powerboosters", false) == true) com.operator.companion.pb.PbStore.openRequested.value = true

        NetworkGate.start(this)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = Color(0xFF07090E)
                ) {
                    // internet is required to open and play — wait here (auto-continues when back online)
                    val online by NetworkGate.online.collectAsState()
                    if (!online) { OfflineScreen(); return@Surface }
                    val tick = resumeTick.intValue
                    val missing = remember(tick) { PermissionSetup.missing(this) }
                    if (missing.isNotEmpty() && !setupSkipped.value) {
                        SetupScreen(
                            missing = missing,
                            onAllowAll = { continueSetup(userTapped = true) },
                            onSkip = {
                                getSharedPreferences("setup", Context.MODE_PRIVATE).edit().putBoolean("skipped", true).apply()
                                setupSkipped.value = true
                                startIfReady() // the phone is reachable now; grant the rest later
                            }
                        )
                        return@Surface
                    }
                    CompanionAppScreen(
                        resumeTick = tick,
                        onRequestPermissions = { requestHardwarePermissions() },
                        onOpenAccessibility = {
                            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                        },
                        onConnectServer = { urls, token ->
                            RelayConfig.save(this, urls, token)
                            RelayService.start(this, restart = RelayConfig.consumeDirty(this))
                        },
                        onStopServer = { RelayService.stop(this) },
                        onStorageAccess = { requestStorageAccess() },
                        onBatteryExemption = { requestBatteryExemption() }
                    )
                }
            }
        }
    }

    fun requestHardwarePermissions() {
        ActivityCompat.requestPermissions(this, getRequiredPermissions(), 101)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handlePairing(intent)
        if (intent.getBooleanExtra("powerboosters", false)) com.operator.companion.pb.PbStore.openRequested.value = true
    }

    override fun onResume() {
        super.onResume()
        resumeTick.intValue++
        NetworkGate.recheck(this)   // e.g. back from network settings
        com.operator.companion.network.CompanionWebSocketClient.activeInstance?.refreshCapabilities()
        // a downloaded update is waiting: show Android's "Update" screen now that the app is in front
        com.operator.companion.relay.AppUpdater.pendingConfirm?.let { runCatching { startActivity(it) } }
        // the laptop asked to uninstall and Android needs the tap: show its confirm now
        com.operator.companion.relay.AppLifecycle.pendingUninstall?.let { runCatching { startActivity(it) } }
        if (chaining) {
            // Back from one of Android's permission screens: go straight to the next one
            window.decorView.postDelayed({ continueSetup(userTapped = false) }, 350)
            return
        }
        startIfReady()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        resumeTick.intValue++
    }

    // ---------- one-tap setup: walk through every permission screen in turn ----------
    private var chaining = false
    private var lastLaunched: PermissionSetup.Step? = null

    private fun continueSetup(userTapped: Boolean) {
        val next = PermissionSetup.missing(this).firstOrNull()
        if (next == null) {
            chaining = false
            resumeTick.intValue++
            startIfReady()
            return
        }
        if (!userTapped && next == lastLaunched) {
            // The person came back without switching it on: stop and let them tap again
            chaining = false
            resumeTick.intValue++
            return
        }
        chaining = true
        lastLaunched = next
        PermissionSetup.launch(this, next)
    }

    /** Covers app launch, boot-less starts and "Pair via USB" (PairReceiver saved new data, laptop opened us).
     *  Connects as soon as the phone is paired — missing feature permissions no longer block the link. */
    private fun startIfReady() {
        if (RelayConfig.enabled(this) && RelayConfig.urls(this).isNotEmpty()) {
            RelayService.start(this, restart = RelayConfig.consumeDirty(this))
        }
    }

    /** acc://pair?u=…&t=… from the laptop's QR code. Always confirmed by the user: links can come from anywhere. */
    private fun handlePairing(intent: Intent?) {
        if (intent == null || intent.flags and Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY != 0) return
        val (urls, token) = RelayConfig.parsePairUri(intent.data) ?: return
        intent.data = null // never re-apply on rotation / relaunch
        val hosts = urls.joinToString("\n") { runCatching { Uri.parse(it).authority }.getOrNull() ?: it }
        android.app.AlertDialog.Builder(this)
            .setTitle("Link to this base?")
            .setMessage("$hosts\n\nThis base will be able to browse, download, upload and delete files on this device. " +
                "Only continue if you scanned this code yourself.")
            .setPositiveButton("Connect") { _, _ ->
                RelayConfig.save(this, urls, token)
                RelayService.start(this, restart = RelayConfig.consumeDirty(this))
                resumeTick.intValue++
                if (!RemoteFileManager.hasStorageAccess(this)) requestStorageAccess()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun requestStorageAccess() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val perApp = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$packageName"))
            runCatching { startActivity(perApp) }.onFailure {
                startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            }
        } else {
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.WRITE_EXTERNAL_STORAGE), 102
            )
        }
    }

    @android.annotation.SuppressLint("BatteryLife")
    private fun requestBatteryExemption() {
        runCatching {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }.onFailure { startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
    }
}
