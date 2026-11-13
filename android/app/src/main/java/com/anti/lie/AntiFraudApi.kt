package com.anti.lie

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class AntiFraudApi(
    baseUrl: String,
    private val token: String,
    private val deviceId: String,
) {
    private val root = baseUrl.trim().trimEnd('/')

    suspend fun health(): JSONObject = withContext(Dispatchers.IO) {
        request("GET", "/v1/health")
    }

    suspend fun check(text: String, onProgress: (JSONObject) -> Unit): JSONObject {
        val lines = text.lineSequence().map(String::trim).filter(String::isNotEmpty).toList()
        require(lines.isNotEmpty()) { "请先输入需要核实的内容" }
        require(lines.size <= 200) { "内容最多支持 200 行" }
        require(lines.all { it.length <= 500 }) { "每行内容最多 500 字" }

        val request = JSONObject()
            .put("source", "in_app")
            .put("client", JSONObject().put("app_version", "0.1.0"))
            .put("items", JSONArray().put(
                JSONObject()
                    .put("type", "chat")
                    .put("origin", "pasted_text")
                    .put("lines", JSONArray().apply {
                        lines.forEachIndexed { index, line ->
                            put(JSONObject()
                                .put("line_id", "0-$index")
                                .put("speaker", "unknown")
                                .put("text", PrivacyRedactor.redact(line)))
                        }
                    }),
            ))

        val created = withContext(Dispatchers.IO) {
            request("POST", "/v1/checks", request, deviceId)
        }
        val checkId = created.getString("check_id")
        val deadline = System.currentTimeMillis() + 180_000L
        var waitMs = created.optLong("poll_after_ms", 800L).coerceIn(250L, 5_000L)
        while (System.currentTimeMillis() < deadline) {
            delay(waitMs)
            val status = withContext(Dispatchers.IO) { request("GET", "/v1/checks/$checkId") }
            onProgress(status)
            when (status.optString("status")) {
                "succeeded" -> return status
                "failed" -> throw ApiException(status.optJSONObject("error")?.optString("message").orEmpty().ifBlank { "这次鉴定没有完成，请重试" })
            }
            waitMs = status.optLong("poll_after_ms", waitMs).coerceIn(250L, 5_000L)
        }
        throw ApiException("分析时间较长，请检查服务状态后重试")
    }

    suspend fun sendFeedback(checkId: String, label: String) {
        withContext(Dispatchers.IO) {
            request("POST", "/v1/checks/$checkId/feedback", JSONObject().put("label", label), deviceId)
        }
    }

    private fun request(
        method: String,
        path: String,
        body: JSONObject? = null,
        device: String? = null,
    ): JSONObject {
        val connection = (URL(root + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 8_000
            readTimeout = 15_000
            setRequestProperty("Authorization", "Bearer $token")
            setRequestProperty("Accept", "application/json")
            device?.let { setRequestProperty("X-Device-Id", it) }
            if (body != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
            }
        }
        try {
            if (body != null) connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val code = connection.responseCode
            if (code !in 200..299) {
                val stream = connection.errorStream
                val errorBody = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }
                val message = runCatching {
                    JSONObject(errorBody.orEmpty()).getJSONObject("error").getString("message")
                }.getOrNull()
                throw ApiException(message ?: "服务返回异常（$code），请检查地址、令牌和网络")
            }
            if (code == HttpURLConnection.HTTP_NO_CONTENT) return JSONObject()
            return connection.inputStream.bufferedReader(Charsets.UTF_8).use { JSONObject(it.readText()) }
        } catch (error: ApiException) {
            throw error
        } catch (error: Exception) {
            throw ApiException("无法连接服务，请确认服务已启动且地址可访问")
        } finally {
            connection.disconnect()
        }
    }
}

class ApiException(message: String) : Exception(message)