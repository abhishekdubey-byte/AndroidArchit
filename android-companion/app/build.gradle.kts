import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21"
}

android {
    namespace = "com.operator.companion"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.operator.companion"
        minSdk = 26
        targetSdk = 35
        versionCode = 49
        versionName = "1.9.21"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    // Permanent release key (kept out of version control): lets every phone install the app normally
    // and accept future versions as updates.
    val keystoreProps = Properties().apply {
        val f = rootProject.file("keystore.properties")
        if (f.exists()) f.inputStream().use { load(it) }
    }
    // Laptop server baked into the app (companion.properties, not in version control):
    // a freshly installed app connects by itself after setup — nothing to type.
    val companionProps = Properties().apply {
        val f = rootProject.file("companion.properties")
        if (f.exists()) f.inputStream().use { load(it) }
    }
    fun quoted(v: String?) = "\"" + (v ?: "").replace("\\", "\\\\").replace("\"", "\\\"") + "\""
    defaultConfig {
        buildConfigField("String", "DEFAULT_URLS", quoted(companionProps.getProperty("serverUrls")))
        buildConfigField("String", "DEFAULT_TOKEN", quoted(companionProps.getProperty("deviceToken")))
    }

    signingConfigs {
        create("release") {
            if (keystoreProps.isNotEmpty()) {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
            enableV1Signing = true
            enableV2Signing = true
            enableV3Signing = true
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("release")
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }
    kotlinOptions {
        jvmTarget = "21"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }

    // full: everything incl. remote control (Accessibility) — install over USB.
    // lite: no Accessibility Service, so Google Play Protect lets it install from WhatsApp/Files on any phone.
    // Same package + key: a Lite phone can be upgraded to Full later without uninstalling.
    flavorDimensions += "edition"
    productFlavors {
        create("full") {
            dimension = "edition"
            buildConfigField("boolean", "LITE", "false")
        }
        create("lite") {
            dimension = "edition"
            versionNameSuffix = "-lite"
            buildConfigField("boolean", "LITE", "true")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.activity:activity-compose:1.9.3")

    // Jetpack Compose BOM
    val composeBom = platform("androidx.compose:compose-bom:2024.10.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")

    // WebSocket Client (OkHttp)
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // JSON Parsing
    implementation("org.json:json:20231013")

    // CameraX
    val cameraxVersion = "1.4.0"
    implementation("androidx.camera:camera-core:$cameraxVersion")
    implementation("androidx.camera:camera-camera2:$cameraxVersion")
    implementation("androidx.camera:camera-lifecycle:$cameraxVersion")
    implementation("androidx.camera:camera-view:$cameraxVersion")

    // Coroutines
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
