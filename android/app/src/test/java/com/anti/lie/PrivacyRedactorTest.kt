package com.anti.lie

import org.junit.Assert.assertEquals
import org.junit.Test

class PrivacyRedactorTest {
    @Test
    fun redactsSensitiveValuesAndPreservesUrls() {
        val source = "联系13800138000，先转200块，链接 https://example.com/200元"

        assertEquals(
            "联系<PHONE>，先转<AMOUNT:200>，链接 https://example.com/200元",
            PrivacyRedactor.redact(source),
        )
    }

    @Test
    fun validatesIdentityAndCardBeforeRedacting() {
        assertEquals("<ID> <CARD>", PrivacyRedactor.redact("11010519491231002X 4111111111111111"))
    }

    @Test
    fun redactionIsIdempotent() {
        val once = PrivacyRedactor.redact("电话 +86 13800138000，金额￥1.2万")

        assertEquals("电话 <PHONE>，金额<AMOUNT:12000>", once)
        assertEquals(once, PrivacyRedactor.redact(once))
    }
}