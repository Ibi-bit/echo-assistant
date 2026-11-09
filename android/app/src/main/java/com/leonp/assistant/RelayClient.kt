package com.leonp.assistant

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class RelayClient(
    private val url: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onOpen(client: RelayClient)
        fun onEvent(client: RelayClient, event: JSONObject)
        fun onAudio(client: RelayClient, pcm: ByteArray)
        fun onClosed(client: RelayClient, reason: String, willRetry: Boolean)
    }

    private val http = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    private var socket: WebSocket? = null

    @Volatile
    private var open = false

    fun connect() {
        val request = Request.Builder().url(url).build()
        socket = http.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                open = true
                listener.onOpen(this@RelayClient)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val event = try {
                    JSONObject(text)
                } catch (_: Exception) {
                    return
                }
                listener.onEvent(this@RelayClient, event)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (bytes.size > 1 && bytes[0] == 0x01.toByte()) {
                    listener.onAudio(this@RelayClient, bytes.substring(1).toByteArray())
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                open = false
                listener.onClosed(this@RelayClient, "closed $code $reason", code != 1000)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                open = false
                listener.onClosed(this@RelayClient, t.message ?: "connection failed", true)
            }
        })
    }

    fun close() {
        open = false
        socket?.close(1000, "bye")
        socket = null
    }

    fun sendAudio(pcm: ByteArray): Boolean {
        val ws = socket ?: return false
        if (!open) return false
        return ws.send(pcm.toByteString())
    }

    fun sendJson(json: String): Boolean = socket?.send(json) ?: false
}
