package main

import (
	"context"
	"testing"
	"time"
)

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

func TestAccountPoolCookieBackedAcquireSkipsTokenOnlyAccounts(t *testing.T) {
	pool := NewAccountPool(NewJSONStore(t.TempDir()+"/accounts.json", []any{}), Settings{MaxInflightPerAccount: 1}, nil)
	pool.accounts = []*Account{
		{Email: "plain@example.com", Token: "plain", Valid: true, StatusCode: "valid"},
		{Email: "cookie@example.com", Token: "cookie", Cookies: "cna=abc", Valid: true, StatusCode: "valid"},
	}
	pool.resetLocked()

	acc := pool.pickLockedForOptions("", accountUsageVideo, true)
	if acc == nil || acc.Email != "cookie@example.com" {
		t.Fatalf("expected media account to require cookies, got %#v", acc)
	}
}

func TestCookieRefreshCandidatesReturnValidTokenOnlyAccounts(t *testing.T) {
	pool := NewAccountPool(NewJSONStore(t.TempDir()+"/accounts.json", []any{}), Settings{MaxInflightPerAccount: 1}, nil)
	pool.accounts = []*Account{
		{Email: "invalid@example.com", Token: "bad", Valid: false, StatusCode: "invalid"},
		{Email: "cookie@example.com", Token: "cookie", Cookies: "cna=abc", Valid: true, StatusCode: "valid"},
		{Email: "plain@example.com", Token: "plain", Valid: true, StatusCode: "valid"},
	}

	candidates := pool.CookieRefreshCandidates(2)
	if len(candidates) != 1 || candidates[0].Email != "plain@example.com" {
		t.Fatalf("expected only valid token-only account candidate, got %#v", candidates)
	}
}

func TestMediaSlotSerializesConcurrentMediaWork(t *testing.T) {
	app := &App{mediaSlots: make(chan struct{}, 1)}
	release, err := app.acquireMediaSlot(contextBackgroundForTest())
	if err != nil {
		t.Fatalf("unexpected slot acquire error: %v", err)
	}
	acquired := make(chan bool, 1)
	go func() {
		release2, err := app.acquireMediaSlot(contextBackgroundForTest())
		if err == nil {
			release2()
			acquired <- true
			return
		}
		acquired <- false
	}()
	select {
	case <-acquired:
		t.Fatal("second media slot should wait until the first slot is released")
	default:
	}
	release()
	if !<-acquired {
		t.Fatal("second media slot should acquire after release")
	}
}

func TestVideoTaskResponseIncludesRetryAfterForActiveTasks(t *testing.T) {
	task := &VideoTask{ID: "video_task_1", Status: "running", Object: videoTaskObjectType, Kind: accountUsageVideo}
	response := videoTaskResponse(task)
	if response["retry_after"] != 10 {
		t.Fatalf("expected retry_after=10 for active task, got %#v", response["retry_after"])
	}
}

func TestImageTaskResponseUsesImagePollURL(t *testing.T) {
	task := &VideoTask{ID: "image_task_1", Status: "queued", Object: imageTaskObjectType, Kind: accountUsageImage}
	response := videoTaskResponse(task)
	if response["poll_url"] != "/v1/images/tasks/image_task_1" {
		t.Fatalf("expected image poll url, got %#v", response["poll_url"])
	}
	if response["kind"] != accountUsageImage {
		t.Fatalf("expected image kind, got %#v", response["kind"])
	}
}

func TestWaitMediaPaceAppliesMinimumInterval(t *testing.T) {
	app := &App{settings: Settings{MediaMinIntervalMS: 20}}
	if err := app.waitMediaPace(contextBackgroundForTest()); err != nil {
		t.Fatalf("unexpected first pace error: %v", err)
	}
	start := time.Now()
	if err := app.waitMediaPace(contextBackgroundForTest()); err != nil {
		t.Fatalf("unexpected second pace error: %v", err)
	}
	if elapsed := time.Since(start); elapsed < 15*time.Millisecond {
		t.Fatalf("expected media pace wait, elapsed=%s", elapsed)
	}
}

func TestUpstreamMediaFileTypeMapsQwenUploadTypes(t *testing.T) {
	cases := map[string]string{
		"image/png":       "image",
		"audio/mpeg":      "audio",
		"video/mp4":       "video",
		"application/pdf": "file",
		"":                "file",
	}
	for contentType, expected := range cases {
		if got := upstreamMediaFileType(contentType); got != expected {
			t.Fatalf("expected %q for %q, got %q", expected, contentType, got)
		}
	}
}

func TestBuildUpstreamRemoteRefForParsedDocument(t *testing.T) {
	ref := buildUpstreamRemoteRef(UploadedLocalFileRecord{Filename: "manual.pdf"}, 128, "application/pdf", "file_1", "user/manual.pdf", "bucket", "oss-cn.example.com", "success", false)
	if ref["type"] != "file" || ref["showType"] != "file" || ref["file_class"] != "document" {
		t.Fatalf("unexpected document ref classification: %#v", ref)
	}
	file, _ := ref["file"].(map[string]any)
	meta, _ := file["meta"].(map[string]any)
	parseMeta, ok := meta["parse_meta"].(map[string]any)
	if !ok || parseMeta["parse_status"] != "success" {
		t.Fatalf("expected parse metadata for document ref, got %#v", meta)
	}
}

func TestBuildUpstreamRemoteRefForI2VImageSkipsParseMetadata(t *testing.T) {
	ref := buildUpstreamRemoteRef(UploadedLocalFileRecord{Filename: "frame.png"}, 256, "image/png", "file_2", "user/frame.png", "bucket", "oss-cn.example.com", "", true)
	if ref["type"] != "image" || ref["showType"] != "image" || ref["file_class"] != "vision" {
		t.Fatalf("unexpected image ref classification: %#v", ref)
	}
	if ref["file_type"] != "image/png" || ref["status"] != "uploaded" {
		t.Fatalf("unexpected image ref fields: %#v", ref)
	}
	file, _ := ref["file"].(map[string]any)
	meta, _ := file["meta"].(map[string]any)
	if _, exists := meta["parse_meta"]; exists {
		t.Fatalf("i2v image ref must not include document parse metadata: %#v", meta)
	}
}

func contextBackgroundForTest() context.Context {
	return context.Background()
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
