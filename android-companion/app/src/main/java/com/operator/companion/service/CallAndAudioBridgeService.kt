package com.operator.companion.service

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.*
import android.os.Build
import android.telecom.TelecomManager
import android.telephony.PhoneStateListener
import android.telephony.TelephonyCallback
import android.telephony.TelephonyManager
import android.util.Base64
import android.util.Log
import androidx.core.content.ContextCompat
import kotlinx.coroutines.*
import java.util.concurrent.Executors

class CallAndAudioBridgeService(
    private val context: Context,
    private val onTelephonyEvent: (eventType: String, phoneNumber: String) -> Unit,
    private val onAudioCaptured: (base64Pcm: String) -> Unit
) {
    private val TAG = "CallAudioBridge"
    private val telecomManager = context.getSystemService(Context.TELECOM_SERVICE) as TelecomManager
    private val telephonyManager = context.getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    private var isStreaming = false
    private var recordThread: Thread? = null
    private var audioRecord: AudioRecord? = null
    private var audioTrack: AudioTrack? = null

    private val sampleRate = 16000
    private val channelIn = AudioFormat.CHANNEL_IN_MONO
    private val channelOut = AudioFormat.CHANNEL_OUT_MONO
    private val audioEncoding = AudioFormat.ENCODING_PCM_16BIT

    init {
        setupTelephonyListener()
    }

    @SuppressLint("MissingPermission")
    private fun setupTelephonyListener() {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.READ_PHONE_STATE) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "READ_PHONE_STATE permission not granted, skipping telephony listener")
            return
        }

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val callback = object : TelephonyCallback(), TelephonyCallback.CallStateListener {
                    override fun onCallStateChanged(state: Int) {
                        handleCallState(state, "")
                    }
                }
                telephonyManager.registerTelephonyCallback(context.mainExecutor, callback)
            } else {
                @Suppress("DEPRECATION")
                val listener = object : PhoneStateListener() {
                    @Deprecated("Deprecated in Java")
                    override fun onCallStateChanged(state: Int, phoneNumber: String?) {
                        handleCallState(state, phoneNumber ?: "")
                    }
                }
                @Suppress("DEPRECATION")
                telephonyManager.listen(listener, PhoneStateListener.LISTEN_CALL_STATE)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error registering telephony listener", e)
        }
    }

    private fun handleCallState(state: Int, number: String) {
        when (state) {
            TelephonyManager.CALL_STATE_RINGING -> {
                Log.d(TAG, "Telephony State: RINGING ($number)")
                onTelephonyEvent("INCOMING_CALL", number.ifBlank { "Incoming Call" })
            }
            TelephonyManager.CALL_STATE_OFFHOOK -> {
                Log.d(TAG, "Telephony State: OFFHOOK (Active Call)")
                enableSpeakerphone()
                startAudioStreaming()
                onTelephonyEvent("CALL_ACTIVE", number)
            }
            TelephonyManager.CALL_STATE_IDLE -> {
                Log.d(TAG, "Telephony State: IDLE")
                stopAudioStreaming()
                resetAudioMode()
                onTelephonyEvent("CALL_ENDED", "")
            }
        }
    }

    @SuppressLint("MissingPermission")
    fun answerCall() {
        Log.d(TAG, "Answering incoming call via TelecomManager...")
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                if (ContextCompat.checkSelfPermission(context, Manifest.permission.ANSWER_PHONE_CALLS) == PackageManager.PERMISSION_GRANTED) {
                    telecomManager.acceptRingingCall()
                } else {
                    Log.w(TAG, "ANSWER_PHONE_CALLS not granted")
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to accept call via telecomManager", e)
        }
        enableSpeakerphone()
        startAudioStreaming()
    }

    @SuppressLint("MissingPermission")
    fun endCall() {
        Log.d(TAG, "Ending call via TelecomManager...")
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                if (ContextCompat.checkSelfPermission(context, Manifest.permission.ANSWER_PHONE_CALLS) == PackageManager.PERMISSION_GRANTED) {
                    telecomManager.endCall()
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to end call via telecomManager", e)
        }
        stopAudioStreaming()
        resetAudioMode()
    }

    fun enableSpeakerphone() {
        try {
            audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
            audioManager.isSpeakerphoneOn = true
            Log.d(TAG, "Speakerphone enabled (MODE_IN_COMMUNICATION)")
        } catch (e: Exception) {
            Log.e(TAG, "Error enabling speakerphone", e)
        }
    }

    fun resetAudioMode() {
        try {
            audioManager.isSpeakerphoneOn = false
            audioManager.mode = AudioManager.MODE_NORMAL
            Log.d(TAG, "Audio mode reset to MODE_NORMAL")
        } catch (e: Exception) {
            Log.e(TAG, "Error resetting audio mode", e)
        }
    }

    @SuppressLint("MissingPermission")
    @Synchronized
    fun startAudioStreaming() {
        if (isStreaming) return
        isStreaming = true

        // 1. Initialize AudioTrack for playing laptop user's voice to caller
        initAudioTrack()

        // 2. Initialize AudioRecord to capture caller voice and stream to laptop
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "RECORD_AUDIO not granted, cannot record caller audio")
            return
        }

        recordThread = Thread {
            try {
                val minBufferSize = AudioRecord.getMinBufferSize(sampleRate, channelIn, audioEncoding)
                val bufferSize = maxOf(minBufferSize, 2048)
                val record = AudioRecord(
                    MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                    sampleRate,
                    channelIn,
                    audioEncoding,
                    bufferSize
                )
                audioRecord = record

                if (record.state != AudioRecord.STATE_INITIALIZED) {
                    Log.e(TAG, "AudioRecord failed to initialize")
                    return@Thread
                }

                record.startRecording()
                Log.d(TAG, "AudioRecord started (16kHz PCM)")

                val buffer = ByteArray(1024)
                while (isStreaming) {
                    val read = record.read(buffer, 0, buffer.size)
                    if (read > 0) {
                        val base64 = Base64.encodeToString(buffer, 0, read, Base64.NO_WRAP)
                        onAudioCaptured(base64)
                    }
                }

                record.stop()
                record.release()
                audioRecord = null
            } catch (e: Exception) {
                Log.e(TAG, "Error in audio recording loop", e)
            }
        }.apply { start() }
    }

    @Synchronized
    fun stopAudioStreaming() {
        isStreaming = false
        try {
            audioRecord?.stop()
            audioRecord?.release()
            audioRecord = null
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping AudioRecord", e)
        }
        try {
            audioTrack?.stop()
            audioTrack?.release()
            audioTrack = null
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping AudioTrack", e)
        }
        Log.d(TAG, "Audio streaming stopped")
    }

    private fun initAudioTrack() {
        try {
            val minBuf = AudioTrack.getMinBufferSize(sampleRate, channelOut, audioEncoding)
            val bufSize = maxOf(minBuf, 2048)

            val track = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(audioEncoding)
                        .setSampleRate(sampleRate)
                        .setChannelMask(channelOut)
                        .build()
                )
                .setBufferSizeInBytes(bufSize)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()

            track.play()
            audioTrack = track
            Log.d(TAG, "AudioTrack initialized (16kHz PCM voice playback)")
        } catch (e: Exception) {
            Log.e(TAG, "Error initializing AudioTrack", e)
        }
    }

    /**
     * Plays PCM audio bytes sent from laptop microphone
     */
    fun playLaptopAudio(pcmBase64: String) {
        try {
            val bytes = Base64.decode(pcmBase64, Base64.DEFAULT)
            if (audioTrack == null || audioTrack?.playState != AudioTrack.PLAYSTATE_PLAYING) {
                initAudioTrack()
            }
            audioTrack?.write(bytes, 0, bytes.size)
        } catch (e: Exception) {
            Log.e(TAG, "Error playing laptop audio chunk", e)
        }
    }
}
