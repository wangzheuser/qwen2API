package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"path/filepath"
	"strings"
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

// TestShouldForceToolContinuationAllowsConciseFinalAfterSilentVerification covers quiet checks such as diff.
func TestShouldForceToolContinuationAllowsConciseFinalAfterSilentVerification(t *testing.T) {
	req := StandardRequest{
		Prompt: stringsJoinForTest(
			"Human: verify the files with diff, then answer exactly CODEX-LOCAL-OK",
			"",
			"Assistant: <|QNML|tool_calls><|QNML|invoke name=\"exec_command\"><|QNML|parameter name=\"cmd\"><![CDATA[diff input.txt output.txt]]></|QNML|parameter></|QNML|invoke></|QNML|tool_calls>",
			"",
			"[Tool Result id=call_123]",
			"",
			"[/Tool Result]",
			"",
			"[STATE NOTICE: MUST OBEY]",
			"The latest client message is a tool result, not a new user request.",
			"Assistant:",
		),
		Tools:                     []map[string]any{{"name": "exec_command"}},
		ToolNames:                 []string{"exec_command"},
		ToolEnabled:               true,
		LatestMessageIsToolResult: true,
	}
	result := CompletionResult{AnswerText: "CODEX-LOCAL-OK"}

	if shouldForceToolContinuation(req, result) {
		t.Fatal("concise final text after a successful silent verification should be accepted")
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

func TestUpstreamBusyDoesNotConsumeAccountQuota(t *testing.T) {
	pool := NewAccountPool(NewJSONStore(t.TempDir()+"/accounts.json", []any{}), Settings{MaxInflightPerAccount: 1}, nil)
	acc := &Account{Email: "busy@example.com", Token: "token", Cookies: "cna=abc", Valid: true, StatusCode: "valid"}
	pool.accounts = []*Account{acc}
	app := &App{accounts: pool, settings: Settings{RateLimitBaseCooldown: 600}}
	err := errors.New("Qwen upstream error code=quota_limit details=目前服务访问量较大，请稍后再试。")

	app.classifyAccountErrorFor(acc, err, accountUsageImage)

	if acc.rateLimitedUntilFor(accountUsageImage) != 0 {
		t.Fatal("upstream congestion must not consume account image quota")
	}
	circuitErr := app.mediaCircuitError()
	if circuitErr == nil {
		t.Fatal("upstream congestion should open the media circuit")
	}
	if status := upstreamMediaErrorStatus(circuitErr); status != 503 {
		t.Fatalf("expected active upstream congestion circuit status 503, got %d", status)
	}
	if isRateLimitErrorMessage(err.Error()) {
		t.Fatal("upstream congestion must not be classified as an account rate limit")
	}
	if status := upstreamMediaErrorStatus(err); status != 503 {
		t.Fatalf("expected upstream congestion status 503, got %d", status)
	}
}

// TestCompletionErrorPolicy 验证上游错误的重试、状态码和脱敏策略保持一致。
func TestCompletionErrorPolicy(t *testing.T) {
	busy := errors.New("Qwen upstream error code=quota_limit request_id=hidden details=目前服务访问量较大，请稍后再试。")
	if !shouldRetryCompletion(CompletionResult{}, busy) {
		t.Fatal("zero-event upstream congestion should retry")
	}
	if shouldRetryCompletion(CompletionResult{Events: []UpstreamEvent{{Type: "delta"}}}, busy) {
		t.Fatal("an emitted upstream event must disable retry")
	}
	waf := errors.New("stream_chat blocked by Aliyun WAF")
	if shouldRetryCompletion(CompletionResult{}, waf) {
		t.Fatal("WAF rejection must not be retried immediately")
	}
	if status := completionErrorStatus(busy); status != http.StatusServiceUnavailable {
		t.Fatalf("expected busy status 503, got %d", status)
	}

	invalid := errors.New("Qwen upstream error code=invalid_input request_id=hidden details=input invalid")
	if shouldRetryCompletion(CompletionResult{}, invalid) {
		t.Fatal("deterministic invalid input must not retry unchanged")
	}
	if status := completionErrorStatus(invalid); status != http.StatusUnprocessableEntity {
		t.Fatalf("expected invalid input status 422, got %d", status)
	}
	if message := sanitizeClientErrorString(invalid.Error()); message != upstreamInvalidInputClientMessage {
		t.Fatalf("invalid input details were not sanitized: %q", message)
	}
}

func TestCookieRefreshDoesNotExpandCooledPool(t *testing.T) {
	pool := NewAccountPool(NewJSONStore(t.TempDir()+"/accounts.json", []any{}), Settings{MaxInflightPerAccount: 1}, nil)
	acc := &Account{Email: "cooldown@example.com", Token: "token", Cookies: "cna=abc", Valid: true, StatusCode: "valid"}
	acc.setRateLimitFor(accountUsageImage, float64(time.Now().Add(time.Minute).UnixNano())/1e9, "quota")
	pool.accounts = []*Account{acc}
	app := &App{accounts: pool, settings: Settings{MediaCookieRefreshBatch: 5}}

	if app.ensureMediaCookieAccount(context.Background(), accountUsageImage) {
		t.Fatal("cooled Cookie accounts should recover naturally instead of expanding the pool")
	}
}

func TestCanceledMediaRequestUsesClientClosedStatus(t *testing.T) {
	err := fmt.Errorf("wrapped: %w", context.Canceled)
	if status := upstreamMediaErrorStatus(err); status != 499 {
		t.Fatalf("expected canceled request status 499, got %d", status)
	}
}

func TestTokenRefreshInitialDelayHonorsCancellation(t *testing.T) {
	service := &TokenRefreshService{app: &App{settings: Settings{
		TokenRefreshInitialDelaySeconds: 60,
		TokenRefreshCheckInterval:       60,
	}}}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		service.run(ctx)
		close(done)
	}()
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("token refresh startup delay should stop after cancellation")
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

func TestExtractInlineImagePayloadSupportsLLMProtocols(t *testing.T) {
	raw := []byte("image-bytes")
	encoded := base64.StdEncoding.EncodeToString(raw)
	cases := []map[string]any{
		{"type": "image_url", "image_url": map[string]any{"url": "data:image/png;base64," + encoded}},
		{"type": "input_image", "image_url": "data:image/png;base64," + encoded},
		{"type": "image", "source": map[string]any{"type": "base64", "media_type": "image/png", "data": encoded}},
	}
	for _, block := range cases {
		_, contentType, got, fileID, ok, err := extractInlineImagePayload(block)
		if err != nil || !ok || fileID != "" || contentType != "image/png" || string(got) != string(raw) {
			t.Fatalf("unexpected normalized image: type=%q file=%q ok=%v err=%v raw=%q", contentType, fileID, ok, err, got)
		}
	}

	_, _, _, fileID, ok, err := extractInlineImagePayload(map[string]any{"type": "image", "source": map[string]any{"type": "file", "file_id": "file-1"}})
	if err != nil || !ok || fileID != "file-1" {
		t.Fatalf("unexpected Anthropic file image: file=%q ok=%v err=%v", fileID, ok, err)
	}
}

func TestGeminiToChatBodyPreservesInlineImage(t *testing.T) {
	body := geminiToChatBody("qwen3.8-max-preview", map[string]any{"contents": []any{map[string]any{
		"role": "user",
		"parts": []any{
			map[string]any{"inlineData": map[string]any{"mimeType": "image/png", "data": "YWJj"}},
			map[string]any{"text": "describe it"},
		},
	}}}, false)
	messages := anyList(body["messages"])
	message, _ := messages[0].(map[string]any)
	parts := anyList(message["content"])
	image, _ := parts[0].(map[string]any)
	imageURL, _ := image["image_url"].(map[string]any)
	if image["type"] != "image_url" || imageURL["url"] != "data:image/png;base64,YWJj" {
		t.Fatalf("Gemini inline image was not preserved: %#v", parts)
	}
}

func TestPreprocessAttachmentsNormalizesAnthropicImage(t *testing.T) {
	dir := t.TempDir()
	app := &App{
		settings:          Settings{ContextGeneratedDir: filepath.Join(dir, "context")},
		uploadedFileStore: NewJSONStore(filepath.Join(dir, "uploaded_files.json"), []any{}),
	}
	imageBase64 := "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
	payload := map[string]any{
		"messages": []any{map[string]any{
			"role": "user",
			"content": []any{
				map[string]any{"type": "image", "source": map[string]any{"type": "base64", "media_type": "image/png", "data": imageBase64}},
				map[string]any{"type": "text", "text": "describe"},
			},
		}},
	}

	result, err := app.preprocessAttachments(payload, "owner-token")
	if err != nil {
		t.Fatalf("unexpected preprocess error: %v", err)
	}
	if len(result.Attachments) != 1 || result.Attachments[0].ContentType != "image/png" {
		t.Fatalf("unexpected normalized attachments: %#v", result.Attachments)
	}
	messages := anyList(result.Payload["messages"])
	message, _ := messages[0].(map[string]any)
	parts := anyList(message["content"])
	image, _ := parts[0].(map[string]any)
	if image["type"] != "input_image" || image["file_id"] == "" {
		t.Fatalf("Anthropic image was not rewritten: %#v", image)
	}
}

func TestUpstreamFilesForAccountDropsForeignSessionFiles(t *testing.T) {
	requestFile := map[string]any{"id": "request-file"}
	sessionFile := map[string]any{"id": "session-file"}
	record := &SessionAffinityRecord{AccountEmail: "old@example.com", UploadedFiles: []map[string]any{sessionFile}}

	files := upstreamFilesForAccount([]map[string]any{requestFile}, record, "new@example.com")
	if len(files) != 1 || files[0]["id"] != "request-file" {
		t.Fatalf("foreign session files must be dropped after account rebinding: %#v", files)
	}
	files = upstreamFilesForAccount([]map[string]any{requestFile}, record, "old@example.com")
	if len(files) != 2 {
		t.Fatalf("same-account session files should be retained: %#v", files)
	}
}

func TestUpstreamThinkingEnabledForVision(t *testing.T) {
	disabled := false
	got := upstreamThinkingEnabled(StandardRequest{ThinkingEnabled: &disabled, UpstreamFiles: []map[string]any{{"type": "image"}}})
	if got == nil || !*got {
		t.Fatal("vision requests must force upstream thinking mode")
	}
	got = upstreamThinkingEnabled(StandardRequest{ThinkingEnabled: &disabled})
	if got == nil || *got {
		t.Fatal("text requests should preserve the requested thinking mode")
	}
}

// TestNormalizeResponsesToolAcceptsTopLevelFunctionFields covers the official Responses tool shape.
func TestNormalizeResponsesToolAcceptsTopLevelFunctionFields(t *testing.T) {
	tool := normalizeResponsesTool(map[string]any{
		"type":       "function",
		"name":       "get_weather",
		"parameters": map[string]any{"type": "object"},
	})
	if tool == nil {
		t.Fatal("top-level Responses function tool was dropped")
	}
	function, _ := tool["function"].(map[string]any)
	if function["name"] != "get_weather" {
		t.Fatalf("unexpected normalized function: %#v", function)
	}
}

// TestResponsesPreviousResponseRestoresToolHistory verifies stateful tool-result follow-ups.
func TestResponsesPreviousResponseRestoresToolHistory(t *testing.T) {
	app := &App{responses: map[string]storedResponseState{}}
	call := map[string]any{
		"id": "fc_test", "type": "function_call", "status": "completed", "call_id": "call_test",
		"name": "lookup_marker", "arguments": `{"key":"alpha"}`,
	}
	app.rememberResponse("resp_test", "owner-token", []any{"find alpha"}, []map[string]any{call})

	expanded, items := app.expandResponsesBody(map[string]any{
		"previous_response_id": "resp_test",
		"input":                []any{map[string]any{"type": "function_call_output", "call_id": "call_test", "output": "MARKER-7319"}},
	}, "owner-token")
	if len(items) != 3 {
		t.Fatalf("expected restored input, call and output, got %#v", items)
	}
	messages := anyList(responsesToChatBody(expanded)["messages"])
	if len(messages) != 3 {
		t.Fatalf("expected three converted messages, got %#v", messages)
	}
	roles := []string{}
	for _, raw := range messages {
		message, _ := raw.(map[string]any)
		roles = append(roles, stringValue(message, "role", ""))
	}
	if strings.Join(roles, ",") != "user,assistant,tool" {
		t.Fatalf("unexpected restored role sequence: %#v", roles)
	}
}

// TestResponsesPreviousResponseIsTokenScoped prevents conversation disclosure across API keys.
func TestResponsesPreviousResponseIsTokenScoped(t *testing.T) {
	app := &App{responses: map[string]storedResponseState{}}
	app.rememberResponse("resp_private", "owner-token", []any{"private"}, nil)

	_, items := app.expandResponsesBody(map[string]any{
		"previous_response_id": "resp_private",
		"input":                "public",
	}, "other-token")
	if len(items) != 1 || items[0] != "public" {
		t.Fatalf("foreign response history leaked: %#v", items)
	}
}

// TestResponsesTextStreamEmitsLifecycleBeforeDelta covers the Codex SSE state machine contract.
func TestResponsesTextStreamEmitsLifecycleBeforeDelta(t *testing.T) {
	var output bytes.Buffer
	started := 0
	stream := newResponsesTextStream(&output, func() { started++ })
	stream.WriteDelta("LOCAL-")
	stream.WriteDelta("OK")
	item := stream.Finish("LOCAL-OK")

	events := responseEventNamesForTest(output.String())
	want := []string{
		"response.output_item.added",
		"response.content_part.added",
		"response.output_text.delta",
		"response.output_text.delta",
		"response.output_text.done",
		"response.content_part.done",
		"response.output_item.done",
	}
	if started != 1 || strings.Join(events, ",") != strings.Join(want, ",") {
		t.Fatalf("unexpected lifecycle: started=%d events=%#v", started, events)
	}
	if item["status"] != "completed" || item["id"] == "" {
		t.Fatalf("unexpected completed item: %#v", item)
	}
	for _, block := range strings.Split(strings.TrimSpace(output.String()), "\n\n") {
		if strings.Contains(block, "response.output_text.delta") && !strings.Contains(block, `"item_id":"msg_`) {
			t.Fatalf("delta is missing its active item id: %s", block)
		}
	}
}

// TestResponsesToolStreamEmitsAddedBeforeDone covers function-call item ordering.
func TestResponsesToolStreamEmitsAddedBeforeDone(t *testing.T) {
	var output bytes.Buffer
	writeResponsesToolItemEvents(&output, map[string]any{
		"id": "fc_test", "type": "function_call", "status": "completed", "call_id": "call_test",
		"name": "lookup_marker", "arguments": `{"key":"alpha"}`,
	}, 0)
	events := responseEventNamesForTest(output.String())
	want := "response.output_item.added,response.function_call_arguments.delta,response.function_call_arguments.done,response.output_item.done"
	if strings.Join(events, ",") != want {
		t.Fatalf("unexpected tool lifecycle: %#v", events)
	}
}

// TestFilterRepeatedToolCallBlocksSuccessfulPostResultReplay covers one-call tool loops.
func TestFilterRepeatedToolCallBlocksSuccessfulPostResultReplay(t *testing.T) {
	call := ParsedToolCall{Name: "lookup_marker", Input: map[string]any{"key": "alpha"}}
	req := StandardRequest{
		Prompt:                    "[Tool Result id=call_test]\nSTATEFUL-OK\n[/Tool Result]",
		LatestMessageIsToolResult: true,
		RepeatedToolCount:         1,
		RepeatedToolSignature:     parsedToolCallSignature(call),
	}
	kept, blocked := filterRepeatedToolCalls(req, []ParsedToolCall{call})
	if len(kept) != 0 || len(blocked) != 1 {
		t.Fatalf("successful tool result replay was not blocked: kept=%#v blocked=%#v", kept, blocked)
	}

	req.Prompt = "[Tool Result id=call_test]\ntimeout; retry\n[/Tool Result]"
	kept, blocked = filterRepeatedToolCalls(req, []ParsedToolCall{call})
	if len(kept) != 1 || len(blocked) != 0 {
		t.Fatalf("explicit retryable failure was blocked: kept=%#v blocked=%#v", kept, blocked)
	}

	req.Prompt = "[Tool Result id=call_test]\n{\"session_id\":42,\"status\":\"process running\"}\n[/Tool Result]"
	kept, blocked = filterRepeatedToolCalls(req, []ParsedToolCall{call})
	if len(kept) != 1 || len(blocked) != 0 {
		t.Fatalf("long-running tool polling was blocked: kept=%#v blocked=%#v", kept, blocked)
	}
}

// responseEventNamesForTest extracts event names from an SSE transcript.
func responseEventNamesForTest(raw string) []string {
	events := []string{}
	for _, line := range strings.Split(raw, "\n") {
		if strings.HasPrefix(line, "event: ") {
			events = append(events, strings.TrimPrefix(line, "event: "))
		}
	}
	return events
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
