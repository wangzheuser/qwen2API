package main

import "testing"

func TestShouldForceToolContinuationAllowsGroundedBashMarker(t *testing.T) {
	req := StandardRequest{
		Prompt: stringsJoinForTest(
			"Human: create a file, verify it, then answer exactly TOOL_OK_123",
			"",
			"Assistant: <|QNML|tool_calls><|QNML|invoke name=\"Bash\"><|QNML|parameter name=\"command\"><![CDATA[python3 verify.py]]></|QNML|parameter></|QNML|invoke></|QNML|tool_calls>",
			"",
			"[Tool Result id=toolu_123]",
			"TOOL_OK_123",
			"[/Tool Result]",
			"",
			"[STATE NOTICE: MUST OBEY]",
			"The latest client message is a tool result, not a new user request.",
			"Assistant:",
		),
		Tools:                     []map[string]any{{"name": "Bash"}},
		ToolNames:                 []string{"Bash"},
		ToolEnabled:               true,
		LatestMessageIsToolResult: true,
	}
	result := CompletionResult{AnswerText: "TOOL_OK_123"}

	if shouldForceToolContinuation(req, result) {
		t.Fatal("grounded Bash marker should be accepted as final answer")
	}
}

func TestShouldForceToolContinuationStillRejectsGenericNarration(t *testing.T) {
	req := StandardRequest{
		Prompt: stringsJoinForTest(
			"Human: create a file, verify it, then answer exactly TOOL_OK_123",
			"",
			"Assistant: <|QNML|tool_calls><|QNML|invoke name=\"Write\"><|QNML|parameter name=\"file_path\"><![CDATA[probe.txt]]></|QNML|parameter></|QNML|invoke></|QNML|tool_calls>",
			"",
			"[Tool Result id=toolu_123]",
			"File created successfully at: probe.txt",
			"[/Tool Result]",
			"",
			"[STATE NOTICE: MUST OBEY]",
			"The latest client message is a tool result, not a new user request.",
			"Assistant:",
		),
		Tools:                     []map[string]any{{"name": "Write"}},
		ToolNames:                 []string{"Write"},
		ToolEnabled:               true,
		LatestMessageIsToolResult: true,
	}
	result := CompletionResult{AnswerText: "I will verify it next."}

	if !shouldForceToolContinuation(req, result) {
		t.Fatal("generic narration after a tool result should still trigger continuation recovery")
	}
}

func TestIsUpstreamWAFErrorMessageDetectsQwenBaxiaSignals(t *testing.T) {
	cases := []string{
		`<!doctypehtml><meta name="aliyun_waf_aa"><meta name="aliyun_waf_bb">`,
		`{"ret":["FAIL_SYS_USER_VALIDATE","RGV587_ERROR::SM::请稍后重试"],"data":{"url":"https://chat.qwen.ai/api/v2/chat/completions/_____tmd_____/punish?x5secdata=abc&action=captcha&pureCaptcha="}}`,
		`访问验证 captcha-element`,
	}
	for _, msg := range cases {
		if !isUpstreamWAFErrorMessage(msg) {
			t.Fatalf("expected WAF marker to be detected: %s", msg)
		}
	}
}

func TestQwenHeadersIncludeCurrentWebFingerprint(t *testing.T) {
	headers := qwenHeadersForIdentity(QwenRequestIdentity{Token: "tok", Cookies: "cna=abc"})
	if headers.Get("source") != "web" {
		t.Fatalf("expected source=web, got %q", headers.Get("source"))
	}
	if headers.Get("version") == "" {
		t.Fatal("expected qwen web version header")
	}
	if headers.Get("timezone") == "" {
		t.Fatal("expected timezone header")
	}
	if headers.Get("Cookie") != "cna=abc" {
		t.Fatalf("expected account cookies to be forwarded, got %q", headers.Get("Cookie"))
	}
	if got := headers.Get("sec-ch-ua"); !stringsContainsForTest(got, "Chromium") || stringsContainsForTest(got, "124") {
		t.Fatalf("unexpected sec-ch-ua fingerprint: %q", got)
	}
}

func TestHasQwenVerificationCookieRequiresNonTokenCookie(t *testing.T) {
	if hasQwenVerificationCookie("token=abc; qwen_token=def") {
		t.Fatal("token-only cookies should not be treated as browser verification cookies")
	}
	if !hasQwenVerificationCookie("token=abc; cna=xyz") {
		t.Fatal("non-token qwen cookie should be treated as browser verification cookie")
	}
}

func TestAccountPoolPrefersCookieBackedAccounts(t *testing.T) {
	pool := NewAccountPool(NewJSONStore(t.TempDir()+"/accounts.json", []any{}), Settings{MaxInflightPerAccount: 1}, nil)
	pool.accounts = []*Account{
		{Email: "plain@example.com", Token: "plain", Valid: true, StatusCode: "valid"},
		{Email: "cookie@example.com", Token: "cookie", Cookies: "cna=abc", Valid: true, StatusCode: "valid"},
	}
	pool.resetLocked()

	acc := pool.pickLockedFor("", accountUsageChat)
	if acc == nil || acc.Email != "cookie@example.com" {
		t.Fatalf("expected cookie-backed account first, got %#v", acc)
	}
}

func stringsContainsForTest(value, needle string) bool {
	return len(needle) == 0 || stringsIndexForTest(value, needle) >= 0
}

func stringsIndexForTest(value, needle string) int {
	if needle == "" {
		return 0
	}
	for i := 0; i+len(needle) <= len(value); i++ {
		if value[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}

func stringsJoinForTest(values ...string) string {
	out := ""
	for i, value := range values {
		if i > 0 {
			out += "\n"
		}
		out += value
	}
	return out
}
