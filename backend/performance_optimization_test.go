package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	pw "github.com/playwright-community/playwright-go"
)

// testAccount 构造带指定过期时间的文件账号。
func testAccount(email string, expiresAt int64, cookies string) Account {
	acc := Account{
		Email:      email,
		Password:   "password",
		Token:      makeExpToken(float64(expiresAt)),
		Cookies:    cookies,
		Source:     "file",
		StatusCode: "valid",
	}
	acc.normalize()
	return acc
}

func TestTokenExpiryControlsBusinessEligibility(t *testing.T) {
	now := time.Now().Unix()
	settings := Settings{}
	cases := []struct {
		name   string
		token  string
		usable bool
	}{
		{name: "normal", token: makeExpToken(float64(now + 7200)), usable: true},
		{name: "expiring", token: makeExpToken(float64(now + 60)), usable: true},
		{name: "expired", token: makeExpToken(float64(now - 1)), usable: false},
		{name: "malformed", token: "not-a-jwt", usable: false},
		{name: "missing", token: "", usable: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			acc := Account{Email: tc.name, Token: tc.token, StatusCode: "valid"}
			acc.normalize()
			if got := acc.availableFor(settings, accountUsageChat); got != tc.usable {
				t.Fatalf("usable=%v, want %v, expiry=%d", got, tc.usable, acc.TokenExpiresAt)
			}
		})
	}
}

func TestExpiredAccountIsNeverAcquiredAndProjectedSetUsesHorizon(t *testing.T) {
	now := time.Now()
	settings := Settings{
		MaxInflightPerAccount:    1,
		GlobalMaxInflight:        32,
		MaxQueueSize:             64,
		AccountReadySetThreshold: 2,
		TokenRefreshAheadSeconds: 3600,
	}
	pool := NewAccountPool(NewJSONStore(filepath.Join(t.TempDir(), "accounts.json"), []any{}), settings, nil)
	expired := testAccount("expired@example.com", now.Add(-time.Minute).Unix(), "")
	expiring := testAccount("expiring@example.com", now.Add(30*time.Minute).Unix(), "")
	fresh := testAccount("fresh@example.com", now.Add(2*time.Hour).Unix(), "cna=fresh")
	pool.accounts = []*Account{&expired, &expiring, &fresh}
	pool.resetLocked()
	acc, err := pool.Acquire(context.Background(), "")
	if err != nil {
		t.Fatalf("acquire failed: %v", err)
	}
	defer pool.Release(acc)
	if acc.Email == expired.Email {
		t.Fatal("expired account was selected")
	}
	status := pool.Status()
	if status["projected_usable"] != 1 || status["ready_set_enabled"] != false {
		t.Fatalf("unexpected projected ready set: %#v", status)
	}
}

func TestFastRefreshPriorityAndBatchLimit(t *testing.T) {
	now := time.Now()
	accounts := []Account{
		testAccount("later@example.com", now.Add(2*time.Hour).Unix(), ""),
		testAccount("expired@example.com", now.Add(-time.Hour).Unix(), ""),
		testAccount("media@example.com", now.Add(-2*time.Hour).Unix(), "cna=media"),
		testAccount("earlier@example.com", now.Add(time.Hour).Unix(), ""),
	}
	for index := 0; index < 8; index++ {
		accounts = append(accounts, testAccount(fmt.Sprintf("extra-%d@example.com", index), now.Add(time.Duration(index+3)*time.Hour).Unix(), ""))
	}
	selected, _, pending := selectFastRefreshCandidates(accounts, newTokenRefreshState(), now, 24*3600, 5)
	if !pending || len(selected) != 5 {
		t.Fatalf("selected=%d pending=%v", len(selected), pending)
	}
	want := []string{"media@example.com", "expired@example.com", "earlier@example.com", "later@example.com"}
	for index, email := range want {
		if selected[index].Account.Email != email {
			t.Fatalf("priority[%d]=%s, want %s", index, selected[index].Account.Email, email)
		}
	}
}

func TestFastRefreshRunsWhenMediaReadinessIsMissing(t *testing.T) {
	now := time.Now()
	settings := Settings{
		AccountReadySetThreshold:            1,
		TokenRefreshAheadSeconds:            3600,
		TokenRefreshBatchSize:               5,
		TokenRefreshBatchTimeoutSeconds:     60,
		TokenRefreshCheckInterval:           300,
		TokenRefreshSlowScanIntervalSeconds: 1800,
		TokenRefreshStateFile:               filepath.Join(t.TempDir(), "refresh.json"),
	}
	pool := NewAccountPool(NewJSONStore(filepath.Join(t.TempDir(), "accounts.json"), []any{}), settings, nil)
	fresh := testAccount("fresh@example.com", now.Add(48*time.Hour).Unix(), "")
	pool.accounts = []*Account{&fresh}
	pool.loaded = true
	pool.resetLocked()
	app := &App{settings: settings, accounts: pool}
	service := NewTokenRefreshService(app, nil)
	launches := 0
	fake := &fakeBrowserBatch{}
	service.browserFactory = func(*slog.Logger) (refreshBrowserBatch, error) {
		launches++
		return fake, nil
	}
	service.refreshCookiesWithPage = func(_ context.Context, _ pw.Page, acc Account) (Account, error) {
		acc.Cookies = "cna=recovered"
		return acc, nil
	}
	service.nextFastRunAt = now.Unix()
	service.nextSlowRunAt = now.Add(time.Hour).Unix()
	service.runScheduledBatch(context.Background(), now)
	if launches != 1 || service.Status()["last_batch_selected"] != 1 {
		t.Fatalf("launches=%d status=%#v", launches, service.Status())
	}
	if !hasUsableMediaAccount(pool.Snapshot(), now.Unix()) {
		t.Fatal("media account was not recovered")
	}
}

func TestExpiredCookieAccountDoesNotBlockMediaRecovery(t *testing.T) {
	now := time.Now()
	pool := NewAccountPool(NewJSONStore(filepath.Join(t.TempDir(), "accounts.json"), []any{}), Settings{}, nil)
	expired := testAccount("expired-media@example.com", now.Add(-time.Hour).Unix(), "cna=expired")
	pool.accounts = []*Account{&expired}
	pool.resetLocked()
	if pool.HasCookieBackedAccount() {
		t.Fatal("expired cookie account must not block recovery from a usable token-only account")
	}
}

func TestRefreshBackoffAndBreakerLifecycle(t *testing.T) {
	for failures, want := range map[int]time.Duration{1: time.Hour, 2: 6 * time.Hour, 3: 24 * time.Hour, 5: 24 * time.Hour} {
		got := refreshBackoffDuration(failures, 0)
		if got != want {
			t.Fatalf("failures=%d backoff=%s, want %s", failures, got, want)
		}
		low := refreshBackoffDuration(failures, -0.1)
		high := refreshBackoffDuration(failures, 0.1)
		if low < time.Duration(float64(want)*0.9) || high > time.Duration(float64(want)*1.1) {
			t.Fatalf("jitter out of range: %s..%s", low, high)
		}
	}
	service := &TokenRefreshService{
		app:   &App{settings: Settings{TokenRefreshBreakerFailures: 5, TokenRefreshBreakerCooldownSeconds: 21600}},
		store: &tokenRefreshStateStore{path: filepath.Join(t.TempDir(), "state.json")},
		state: newTokenRefreshState(),
	}
	now := time.Now()
	for index := 0; index < 5; index++ {
		service.recordFailure(fmt.Sprintf("%064d", index), "network", now)
	}
	if service.breakerState() != "open" {
		t.Fatalf("breaker=%s, want open", service.breakerState())
	}
	service.mu.Lock()
	service.state.Breaker.OpenUntil = now.Add(-time.Second).Unix()
	service.mu.Unlock()
	if !service.breakerAllowsAttempt(now) || service.breakerState() != "half_open" {
		t.Fatal("cooldown should allow exactly one half-open probe")
	}
	service.recordSuccess(strings.Repeat("f", 64), now)
	if service.breakerState() != "closed" {
		t.Fatal("successful probe should close breaker")
	}
}

func TestRefreshStateAtomicPersistenceCorruptionAndCursor(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "token_refresh_state.json")
	store := &tokenRefreshStateStore{path: path}
	accountA := testAccount("a@example.com", time.Now().Add(time.Hour).Unix(), "")
	accountB := testAccount("b@example.com", time.Now().Add(time.Hour).Unix(), "")
	state := newTokenRefreshState()
	state.SlowScanCursor = accountRefreshID(accountA)
	state.Accounts[accountRefreshID(accountA)] = refreshAccountState{Failures: 2, LastErrorClass: "network"}
	state.Accounts[strings.Repeat("9", 64)] = refreshAccountState{Failures: 1}
	if err := store.Save(state); err != nil {
		t.Fatalf("save state: %v", err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(raw, []byte(accountA.Email)) || bytes.Contains(raw, []byte(accountA.Token)) {
		t.Fatal("state file leaked raw account identity")
	}
	loaded, err := store.Load([]Account{accountA, accountB})
	if err != nil {
		t.Fatalf("load state: %v", err)
	}
	if len(loaded.Accounts) != 1 || loaded.SlowScanCursor != accountRefreshID(accountA) {
		t.Fatalf("unexpected pruned state: %#v", loaded)
	}
	if err := os.WriteFile(path, []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load([]Account{accountA}); err != nil {
		t.Fatalf("corrupt recovery: %v", err)
	}
	backups, _ := filepath.Glob(path + ".corrupt.*")
	if len(backups) != 1 {
		t.Fatalf("expected one corrupt backup, got %v", backups)
	}
}

type fakeBrowserBatch struct {
	contexts int
	closed   bool
}

func (b *fakeBrowserBatch) WithPage(ctx context.Context, _ Account, fn func(page pw.Page) error) error {
	b.contexts++
	return fn(nil)
}

func (b *fakeBrowserBatch) Close() {
	b.closed = true
}

func TestRefreshBatchReusesBrowserAndPersistsAllSuccesses(t *testing.T) {
	now := time.Now()
	settings := Settings{
		MaxInflightPerAccount:               1,
		GlobalMaxInflight:                   32,
		MaxQueueSize:                        64,
		AccountReadySetThreshold:            128,
		TokenRefreshAheadSeconds:            3600,
		TokenRefreshBatchSize:               5,
		TokenRefreshBatchTimeoutSeconds:     600,
		TokenRefreshCheckInterval:           300,
		TokenRefreshSlowScanIntervalSeconds: 1800,
		TokenRefreshBreakerFailures:         5,
		TokenRefreshBreakerCooldownSeconds:  21600,
		AccountsFile:                        filepath.Join(t.TempDir(), "accounts.json"),
		TokenRefreshStateFile:               filepath.Join(t.TempDir(), "refresh.json"),
	}
	pool := NewAccountPool(NewJSONStore(settings.AccountsFile, []any{}), settings, nil)
	for index := 0; index < 7; index++ {
		cookies := ""
		if index == 0 {
			cookies = "cna=baseline"
		}
		acc := testAccount(fmt.Sprintf("batch-%d@example.com", index), now.Add(time.Minute).Unix(), cookies)
		pool.accounts = append(pool.accounts, &acc)
	}
	pool.loaded = true
	pool.resetLocked()
	app := &App{settings: settings, accounts: pool}
	service := NewTokenRefreshService(app, nil)
	launches := 0
	fake := &fakeBrowserBatch{}
	service.browserFactory = func(*slog.Logger) (refreshBrowserBatch, error) {
		launches++
		return fake, nil
	}
	service.refreshWithPage = func(ctx context.Context, _ pw.Page, acc Account) (Account, error) {
		acc.Token = makeExpToken(float64(time.Now().Add(48 * time.Hour).Unix()))
		acc.normalize()
		return acc, nil
	}
	service.nextFastRunAt = now.Unix()
	service.nextSlowRunAt = now.Add(time.Hour).Unix()
	service.runScheduledBatch(context.Background(), now)
	if launches != 1 || fake.contexts != 5 || !fake.closed {
		t.Fatalf("launches=%d contexts=%d closed=%v", launches, fake.contexts, fake.closed)
	}
	var persisted []Account
	if err := pool.store.LoadInto(&persisted); err != nil {
		t.Fatalf("load persisted accounts: %v", err)
	}
	if len(persisted) != 7 {
		t.Fatalf("persisted=%d, want 7", len(persisted))
	}
	if service.Status()["last_batch_selected"] != 5 {
		t.Fatalf("unexpected status: %#v", service.Status())
	}
}

func TestAccountPoolNotificationQueueFullAndTimeout(t *testing.T) {
	settings := Settings{MaxInflightPerAccount: 1, GlobalMaxInflight: 1, MaxQueueSize: 1, PerformanceReleaseStage: "B"}
	pool := NewAccountPool(NewJSONStore(filepath.Join(t.TempDir(), "accounts.json"), []any{}), settings, nil)
	acc := testAccount("queue@example.com", time.Now().Add(time.Hour).Unix(), "")
	pool.accounts = []*Account{&acc}
	pool.resetLocked()
	first, err := pool.Acquire(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	secondResult := make(chan error, 1)
	go func() {
		second, acquireErr := pool.Acquire(context.Background(), "")
		if acquireErr == nil {
			pool.Release(second)
		}
		secondResult <- acquireErr
	}()
	deadline := time.Now().Add(time.Second)
	for pool.Status()["queued"] != 1 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if _, err := pool.Acquire(context.Background(), ""); !errors.Is(err, errAccountQueueFull) {
		t.Fatalf("queue full error=%v", err)
	}
	pool.Release(first)
	select {
	case err := <-secondResult:
		if err != nil {
			t.Fatalf("notified acquire failed: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("release did not notify queued request")
	}

	empty := NewAccountPool(NewJSONStore(filepath.Join(t.TempDir(), "empty.json"), []any{}), settings, nil)
	previousTimeout := accountAcquireTimeout
	accountAcquireTimeout = 30 * time.Millisecond
	defer func() { accountAcquireTimeout = previousTimeout }()
	if _, err := empty.Acquire(context.Background(), ""); !errors.Is(err, errNoAvailableAccount) {
		t.Fatalf("timeout error=%v", err)
	}
}

func TestBrowserTaskGatePrioritizesManagementWork(t *testing.T) {
	gate := newBrowserTaskGate()
	if err := gate.lock(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	order := make(chan string, 2)
	lowStarted := make(chan struct{})
	go func() {
		close(lowStarted)
		if err := gate.lock(context.Background(), false); err == nil {
			order <- "automatic"
			gate.Unlock()
		}
	}()
	<-lowStarted
	releaseManagement := make(chan struct{})
	go func() {
		if err := gate.lock(context.Background(), true); err == nil {
			order <- "management"
			<-releaseManagement
			gate.Unlock()
		}
	}()
	deadline := time.Now().Add(time.Second)
	for {
		gate.mu.Lock()
		waiting := gate.highWaiters
		gate.mu.Unlock()
		if waiting > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("management task did not enter priority queue")
		}
		time.Sleep(time.Millisecond)
	}
	gate.Unlock()
	if first := <-order; first != "management" {
		t.Fatalf("first task=%s, want management", first)
	}
	close(releaseManagement)
	if second := <-order; second != "automatic" {
		t.Fatalf("second task=%s, want automatic", second)
	}
}

func TestBrowserLaunchArgsControlDevShmFallback(t *testing.T) {
	contains := func(values []string, expected string) bool {
		for _, value := range values {
			if value == expected {
				return true
			}
		}
		return false
	}
	if !contains(browserLaunchArgs(true), "--disable-dev-shm-usage") {
		t.Fatal("release A must keep the /dev/shm fallback argument")
	}
	if contains(browserLaunchArgs(false), "--disable-dev-shm-usage") {
		t.Fatal("release B candidate must remove the /dev/shm fallback argument")
	}
}

func TestBoundedResponseFileAndDeletedCaches(t *testing.T) {
	app := &App{settings: Settings{PerformanceReleaseStage: "B"}, responses: map[string]storedResponseState{}}
	payload := strings.Repeat("x", 1<<20)
	for index := 0; index < 300; index++ {
		app.rememberResponse(fmt.Sprintf("resp-%d", index), "auth", []any{payload}, nil)
	}
	if len(app.responses) > responseStateLimit || app.responsesBytes > responseStateMaxTotal {
		t.Fatalf("responses count=%d bytes=%d", len(app.responses), app.responsesBytes)
	}

	cache := newFileContentCache(true)
	for index := 0; index < fileContentCacheMaxItems+1; index++ {
		cache.Put("auth", fmt.Sprintf("file-%d", index), "content")
	}
	if len(cache.items) != fileContentCacheMaxItems {
		t.Fatalf("file cache count=%d", len(cache.items))
	}
	largeCache := newFileContentCache(true)
	megabyte := strings.Repeat("m", 1<<20)
	for index := 0; index < 40; index++ {
		largeCache.Put("auth", fmt.Sprintf("large-%d", index), megabyte)
	}
	if largeCache.totalBytes > fileContentCacheMaxBytes {
		t.Fatalf("file cache bytes=%d", largeCache.totalBytes)
	}

	client := &QwenClient{settings: Settings{PerformanceReleaseStage: "B"}, deleted: map[string]time.Time{"expired": time.Now().Add(-2 * time.Hour)}}
	client.mu.Lock()
	for index := 0; index < deletedChatLimit+1; index++ {
		client.rememberDeletedChatLocked(fmt.Sprintf("chat-%d", index), time.Now().Add(time.Duration(index)*time.Nanosecond))
	}
	client.mu.Unlock()
	if len(client.deleted) > deletedChatLimit {
		t.Fatalf("deleted cache count=%d", len(client.deleted))
	}
	if _, ok := client.deleted["expired"]; ok {
		t.Fatal("expired deleted-chat marker was retained")
	}
}

func TestProbeLoggingSuppressesSuccessfulHealthAndKeepsFailure(t *testing.T) {
	var buffer bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&buffer, &slog.HandlerOptions{Level: slog.LevelInfo}))
	app := &App{logger: logger}
	success := app.withRequestLogging(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	success.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if strings.Contains(buffer.String(), "请求进入") || strings.Contains(buffer.String(), "请求完成") {
		t.Fatalf("successful health probe emitted INFO: %s", buffer.String())
	}
	buffer.Reset()
	failure := app.withRequestLogging(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	failure.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if !strings.Contains(buffer.String(), "请求完成") {
		t.Fatalf("failed readiness probe was not logged: %s", buffer.String())
	}
}

func TestEmptyContextCleanupDoesNotLogInfo(t *testing.T) {
	var buffer bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&buffer, &slog.HandlerOptions{Level: slog.LevelInfo}))
	dir := t.TempDir()
	app := &App{
		logger:            logger,
		settings:          Settings{ContextGeneratedDir: filepath.Join(dir, "generated"), ContextAttachmentTTLSeconds: 1800},
		uploadedFileStore: NewJSONStore(filepath.Join(dir, "uploaded.json"), []any{}),
		contextCacheStore: NewJSONStore(filepath.Join(dir, "context.json"), []any{}),
		sessionStore:      NewJSONStore(filepath.Join(dir, "sessions.json"), []any{}),
	}
	app.cleanupContextArtifacts(context.Background())
	if strings.Contains(buffer.String(), "上下文缓存清理完成") {
		t.Fatalf("empty cleanup emitted INFO: %s", buffer.String())
	}
}
