package com.operator.companion.admin

import android.app.admin.DeviceAdminReceiver

/**
 * Lets the laptop make this app Device Owner (`dpm set-device-owner …/.admin.CompanionAdminReceiver`),
 * which only serves to install updates and uninstall the app without prompts. It never locks, wipes
 * or sets any policy.
 */
class CompanionAdminReceiver : DeviceAdminReceiver()
