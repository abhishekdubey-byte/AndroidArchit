package com.operator.companion.relay

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings

/**
 * Many phone makers kill background apps on top of stock Android (Realme/OPPO/OnePlus "battery
 * manager", MIUI autostart, Vivo background manager, Samsung sleeping apps...). Only the user can
 * whitelist the app there, so we open the right screen and say exactly what to switch on.
 */
object OemBackground {
    private val brand get() = Build.MANUFACTURER.lowercase()

    val isRestrictive: Boolean
        get() = brand in setOf("realme", "oppo", "oneplus", "xiaomi", "redmi", "poco", "vivo", "iqoo",
            "huawei", "honor", "samsung", "asus", "meizu", "tecno", "infinix", "itel", "lenovo", "motorola")

    val hint: String
        get() = when (brand) {
            "realme", "oppo", "oneplus" -> "App info → Battery usage: turn ON \"Allow background activity\" and \"Allow auto launch\". Also lock the app in Recents."
            "xiaomi", "redmi", "poco" -> "Turn ON Autostart, then App info → Battery saver → \"No restrictions\"."
            "vivo", "iqoo" -> "Allow background start-up / auto-start, then Battery → \"Allow high background power consumption\"."
            "huawei", "honor" -> "App launch: switch to \"Manage manually\" and enable auto-launch, secondary launch and run in background."
            "samsung" -> "App info → Battery → \"Unrestricted\", and remove the app from Sleeping apps."
            else -> "App info → Battery: allow background activity / unrestricted."
        }

    private fun intents(ctx: Context): List<Intent> {
        val details = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${ctx.packageName}"))
        val specific = when (brand) {
            "xiaomi", "redmi", "poco" -> listOf(ComponentName("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"))
            "vivo", "iqoo" -> listOf(ComponentName("com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"),
                ComponentName("com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.BgStartUpManager"))
            "huawei", "honor" -> listOf(ComponentName("com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"))
            else -> emptyList() // ColorOS / Samsung: the toggles live on the app's own info page
        }.map { Intent().setComponent(it) }
        return specific + details
    }

    fun open(ctx: Context) {
        for (i in intents(ctx)) {
            if (runCatching { ctx.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }.isSuccess) return
        }
    }
}
