package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/rand"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	pw "github.com/playwright-community/playwright-go"
)

const refreshStateVersion = 1

type refreshAccountState struct {
	Failures       int    `json:"failures"`
	NextAttemptAt  int64  `json:"next_attempt_at"`
	LastAttemptAt  int64  `json:"last_attempt_at"`
	LastErrorClass string `json:"last_error_class"`
}

type refreshBreakerState struct {
	State               string `json:"state"`
	ErrorClass          string `json:"error_class"`
	ConsecutiveFailures int    `json:"consecutive_failures"`
	OpenUntil           int64  `json:"open_until"`
}

type refreshPersistentState struct {
	Version        int                            `json:"version"`
	SlowScanCursor string                         `json:"slow_scan_cursor"`
	Accounts       map[string]refreshAccountState `json:"accounts"`
	Breaker        refreshBreakerState            `json:"breaker"`
	UpdatedAt      int64                          `json:"updated_at"`
}

type tokenRefreshStateStore struct {
	path   string
	logger *slog.Logger
}

// newTokenRefreshState 返回可独立持久化的空刷新状态。
func newTokenRefreshState() refreshPersistentState {
	return refreshPersistentState{
		Version:  refreshStateVersion,
		Accounts: map[string]refreshAccountState{},
		Breaker:  refreshBreakerState{State: "closed"},
	}
}

// accountRefreshID 使用规范化身份的 SHA-256 标识账号，不持久化原始身份值。
func accountRefreshID(acc Account) string {
	identity := strings.ToLower(strings.TrimSpace(firstNonEmpty(acc.Email, acc.Username, acc.EnvName, acc.Token)))
	sum := sha256.Sum256([]byte(identity))
	return hex.EncodeToString(sum[:])
}

// Load 加载刷新状态，并清理已不在账号池中的哈希记录。
func (s *tokenRefreshStateStore) Load(accounts []Account) (refreshPersistentState, error) {
	state := newTokenRefreshState()
	raw, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return state, nil
	}
	if err != nil {
		return state, err
	}
	if err := json.Unmarshal(raw, &state); err != nil || state.Version != refreshStateVersion {
		backup := fmt.Sprintf("%s.corrupt.%s", s.path, time.Now().UTC().Format("20060102T150405Z"))
		if renameErr := os.Rename(s.path, backup); renameErr != nil {
			return newTokenRefreshState(), fmt.Errorf("preserve corrupt refresh state: %w", renameErr)
		}
		if s.logger != nil {
			s.logger.Warn("token 刷新状态文件损坏，已保留副本并使用空状态启动", "path", s.path, "backup_path", backup)
		}
		return newTokenRefreshState(), nil
	}
	if state.Accounts == nil {
		state.Accounts = map[string]refreshAccountState{}
	}
	if state.Breaker.State == "" {
		state.Breaker.State = "closed"
	}
	active := make(map[string]struct{}, len(accounts))
	for _, acc := range accounts {
		active[accountRefreshID(acc)] = struct{}{}
	}
	for id := range state.Accounts {
		if _, ok := active[id]; !ok {
			delete(state.Accounts, id)
		}
	}
	return state, nil
}

// Save 使用同目录临时文件和 rename 原子保存刷新状态。
func (s *tokenRefreshStateStore) Save(state refreshPersistentState) error {
	state.Version = refreshStateVersion
	state.UpdatedAt = time.Now().Unix()
	if state.Accounts == nil {
		state.Accounts = map[string]refreshAccountState{}
	}
	raw, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(s.path), ".token-refresh-*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(raw); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpPath, s.path)
}

type refreshCandidate struct {
	Account    Account
	ID         string
	Priority   int
	CookieOnly bool
}

// refreshBackoffDuration 返回带固定阶梯和 ±10% jitter 的退避时长。
func refreshBackoffDuration(failures int, jitter float64) time.Duration {
	base := time.Hour
	if failures == 2 {
		base = 6 * time.Hour
	} else if failures >= 3 {
		base = 24 * time.Hour
	}
	if jitter < -0.1 {
		jitter = -0.1
	}
	if jitter > 0.1 {
		jitter = 0.1
	}
	return time.Duration(float64(base) * (1 + jitter))
}

// projectedUsableAccounts 返回刷新窗口结束时仍可用于业务的账号数。
func projectedUsableAccounts(accounts []Account, horizon int64) int {
	count := 0
	for i := range accounts {
		if accounts[i].Valid && accounts[i].TokenExpiresAt > horizon {
			count++
		}
	}
	return count
}

// hasUsableMediaAccount 判断当前是否至少有一个同时具备有效 token 和浏览器 Cookie 的账号。
func hasUsableMediaAccount(accounts []Account, now int64) bool {
	for i := range accounts {
		if accounts[i].businessUsableAt(now) && strings.TrimSpace(accounts[i].Cookies) != "" {
			return true
		}
	}
	return false
}

// selectFastRefreshCandidates 按媒体恢复、已过期和到期时间选择快速通道账号。
func selectFastRefreshCandidates(accounts []Account, state refreshPersistentState, now time.Time, aheadSeconds, limit int, recoverMedia ...bool) ([]refreshCandidate, int, bool) {
	horizon := now.Add(time.Duration(maxInt(aheadSeconds, 0)) * time.Second).Unix()
	mediaRecovery := len(recoverMedia) > 0 && recoverMedia[0]
	candidates := make([]refreshCandidate, 0)
	for _, acc := range accounts {
		if mediaRecovery && acc.businessUsableAt(now.Unix()) && strings.TrimSpace(acc.Cookies) == "" {
			candidates = append(candidates, refreshCandidate{
				Account: acc, ID: accountRefreshID(acc), Priority: -1, CookieOnly: true,
			})
			continue
		}
		if !acc.refreshable() || acc.TokenExpiresAt > horizon {
			continue
		}
		priority := 3
		if strings.TrimSpace(acc.Cookies) != "" {
			priority = 0
		} else if acc.TokenExpiresAt <= now.Unix() {
			priority = 1
		} else if acc.TokenExpiresAt <= horizon {
			priority = 2
		}
		candidates = append(candidates, refreshCandidate{Account: acc, ID: accountRefreshID(acc), Priority: priority})
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].Priority != candidates[j].Priority {
			return candidates[i].Priority < candidates[j].Priority
		}
		if candidates[i].Account.TokenExpiresAt != candidates[j].Account.TokenExpiresAt {
			return candidates[i].Account.TokenExpiresAt < candidates[j].Account.TokenExpiresAt
		}
		return candidates[i].ID < candidates[j].ID
	})
	skipped := 0
	selected := make([]refreshCandidate, 0, min(maxInt(limit, 1), len(candidates)))
	for _, candidate := range candidates {
		if accountState := state.Accounts[candidate.ID]; accountState.NextAttemptAt > now.Unix() {
			skipped++
			continue
		}
		selected = append(selected, candidate)
		if len(selected) >= maxInt(limit, 1) {
			break
		}
	}
	return selected, skipped, len(candidates) > 0
}

// selectSlowRefreshCandidate 按哈希游标选择一个非工作集账号。
func selectSlowRefreshCandidate(accounts []Account, state refreshPersistentState, now time.Time, readyTarget int) (refreshCandidate, int, bool) {
	working := append([]Account(nil), accounts...)
	sort.Slice(working, func(i, j int) bool {
		if working[i].TokenExpiresAt != working[j].TokenExpiresAt {
			return working[i].TokenExpiresAt > working[j].TokenExpiresAt
		}
		return accountRefreshID(working[i]) < accountRefreshID(working[j])
	})
	workingIDs := map[string]struct{}{}
	for _, acc := range working {
		if len(workingIDs) >= maxInt(readyTarget, 1) {
			break
		}
		if acc.businessUsableAt(now.Unix()) {
			workingIDs[accountRefreshID(acc)] = struct{}{}
		}
	}
	candidates := make([]refreshCandidate, 0)
	for _, acc := range accounts {
		id := accountRefreshID(acc)
		if !acc.refreshable() {
			continue
		}
		if _, ok := workingIDs[id]; ok {
			continue
		}
		candidates = append(candidates, refreshCandidate{Account: acc, ID: id, Priority: 4})
	}
	sort.Slice(candidates, func(i, j int) bool { return candidates[i].ID < candidates[j].ID })
	if len(candidates) == 0 {
		return refreshCandidate{}, 0, false
	}
	start := 0
	for index, candidate := range candidates {
		if candidate.ID > state.SlowScanCursor {
			start = index
			break
		}
		if index == len(candidates)-1 {
			start = 0
		}
	}
	skipped := 0
	for offset := 0; offset < len(candidates); offset++ {
		candidate := candidates[(start+offset)%len(candidates)]
		if accountState := state.Accounts[candidate.ID]; accountState.NextAttemptAt > now.Unix() {
			skipped++
			continue
		}
		return candidate, skipped, true
	}
	return refreshCandidate{}, skipped, false
}

type refreshBrowserBatch interface {
	WithPage(context.Context, Account, func(page pw.Page) error) error
	Close()
}

type BrowserManager struct {
	runner        *pw.Playwright
	browser       pw.Browser
	upstreamProxy *UpstreamProxyPool
}

// newBrowserBatch 每批只启动一次 Playwright 和 Chromium。
func newBrowserBatch(logger *slog.Logger, disableDevShmUsage bool, upstreamProxy ...*UpstreamProxyPool) (refreshBrowserBatch, error) {
	if err := installPlaywrightBrowsers(logger); err != nil {
		return nil, fmt.Errorf("browser launch: playwright install: %w", err)
	}
	runner, err := pw.Run(&pw.RunOptions{SkipInstallBrowsers: true})
	if err != nil {
		return nil, fmt.Errorf("browser launch: playwright run: %w", err)
	}
	browser, err := runner.Chromium.Launch(pw.BrowserTypeLaunchOptions{
		Headless: pw.Bool(true),
		Timeout:  pw.Float(60000),
		Args:     browserLaunchArgs(disableDevShmUsage),
	})
	if err != nil {
		_ = runner.Stop()
		return nil, fmt.Errorf("browser launch: chromium: %w", err)
	}
	var proxyPool *UpstreamProxyPool
	if len(upstreamProxy) > 0 {
		proxyPool = upstreamProxy[0]
	}
	return &BrowserManager{runner: runner, browser: browser, upstreamProxy: proxyPool}, nil
}

// browserLaunchArgs 统一生成 Chromium 启动参数，并支持发布 B 的 /dev/shm 验证。
func browserLaunchArgs(disableDevShmUsage bool) []string {
	args := []string{"--disable-blink-features=AutomationControlled", "--no-sandbox"}
	if disableDevShmUsage {
		args = append([]string{"--disable-dev-shm-usage"}, args...)
	}
	return args
}

// WithPage 为单个账号创建隔离 BrowserContext，并在完成或取消时立即关闭。
func (b *BrowserManager) WithPage(ctx context.Context, acc Account, fn func(page pw.Page) error) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	browserContext, err := b.browser.NewContext(pw.BrowserNewContextOptions{
		UserAgent: pw.String("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"),
		Locale:    pw.String("zh-CN"),
		Viewport:  &pw.Size{Width: 1365, Height: 768},
		Proxy:     b.upstreamProxy.playwrightProxyForIdentity(qwenIdentityFromAccount(&acc)),
		ExtraHttpHeaders: map[string]string{
			"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
		},
	})
	if err != nil {
		return fmt.Errorf("browser context: %w", err)
	}
	defer browserContext.Close()
	page, err := browserContext.NewPage()
	if err != nil {
		return fmt.Errorf("browser page: %w", err)
	}
	page.SetDefaultTimeout(30000)
	page.SetDefaultNavigationTimeout(60000)
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			_ = browserContext.Close()
		case <-done:
		}
	}()
	return fn(page)
}

// Close 关闭批次浏览器及 Playwright 驱动。
func (b *BrowserManager) Close() {
	if b == nil {
		return
	}
	if b.browser != nil {
		_ = b.browser.Close()
	}
	if b.runner != nil {
		_ = b.runner.Stop()
	}
}

// classifyRefreshError 将刷新错误归一为有限类别。
func classifyRefreshError(err error) string {
	if err == nil {
		return ""
	}
	lower := strings.ToLower(err.Error())
	switch {
	case strings.Contains(lower, "browser launch"), strings.Contains(lower, "playwright run"), strings.Contains(lower, "chromium"):
		return "browser_launch"
	case strings.Contains(lower, "selector"), strings.Contains(lower, "locator"):
		return "login_selector"
	case strings.Contains(lower, "login timeout"), strings.Contains(lower, "deadline exceeded"), strings.Contains(lower, "timed out"):
		return "login_timeout"
	case strings.Contains(lower, "waf"), strings.Contains(lower, "aliyun"), strings.Contains(lower, "captcha"):
		return "upstream_waf"
	case strings.Contains(lower, "token verify"), strings.Contains(lower, "expired token"):
		return "token_verify"
	case isTransientUpstreamErrorMessage(lower), strings.Contains(lower, "network"):
		return "network"
	default:
		return "other"
	}
}

type tokenRefreshBatchSummary struct {
	Selected       int
	Attempted      int
	Succeeded      int
	Failed         int
	SkippedBackoff int
	Duration       time.Duration
	BreakerState   string
}

type TokenRefreshService struct {
	app    *App
	logger *slog.Logger
	store  *tokenRefreshStateStore

	mu      sync.Mutex
	cancel  context.CancelFunc
	running bool
	loaded  bool
	state   refreshPersistentState

	phase                  string
	fastPending            bool
	lastRun                int64
	lastRefresh            int64
	attemptedTotal         int
	succeededTotal         int
	failedTotal            int
	lastError              string
	lastBatchDurationMS    int64
	lastBatchSelected      int
	nextFastRunAt          int64
	nextSlowRunAt          int64
	pendingAccountResults  []Account
	browserFactory         func(*slog.Logger) (refreshBrowserBatch, error)
	refreshWithPage        func(context.Context, pw.Page, Account) (Account, error)
	refreshCookiesWithPage func(context.Context, pw.Page, Account) (Account, error)
}

// NewTokenRefreshService 创建调度器并加载独立刷新状态。
func NewTokenRefreshService(app *App, logger *slog.Logger) *TokenRefreshService {
	service := &TokenRefreshService{
		app:    app,
		logger: logger,
		store:  &tokenRefreshStateStore{path: app.settings.TokenRefreshStateFile, logger: logger},
		phase:  "idle",
		state:  newTokenRefreshState(),
	}
	service.browserFactory = func(logger *slog.Logger) (refreshBrowserBatch, error) {
		return newBrowserBatch(logger, app.settings.BrowserDisableDevShmUsage, app.upstreamProxy)
	}
	service.refreshWithPage = app.refreshAccountTokenWithPage
	service.refreshCookiesWithPage = app.refreshAccountCookiesWithPage
	state, err := service.store.Load(app.accounts.Snapshot())
	if err != nil {
		service.phase = "error"
		service.lastError = err.Error()
		if logger != nil {
			logger.Warn("token 刷新状态加载失败", "path", app.settings.TokenRefreshStateFile, "error_type", fmt.Sprintf("%T", err))
		}
		return service
	}
	service.state = state
	service.loaded = true
	return service
}

// Start 启动可取消的混合刷新调度器。
func (s *TokenRefreshService) Start(parent context.Context) {
	if s == nil || !s.app.settings.TokenRefreshEnabled || !s.loaded {
		return
	}
	s.mu.Lock()
	if s.cancel != nil {
		s.cancel()
	}
	ctx, cancel := context.WithCancel(parent)
	s.cancel = cancel
	s.running = true
	s.phase = "idle"
	s.mu.Unlock()
	go s.run(ctx)
}

// Stop 停止调度器并保存当前退避和熔断状态。
func (s *TokenRefreshService) Stop() {
	if s == nil {
		return
	}
	s.mu.Lock()
	if s.cancel != nil {
		s.cancel()
		s.cancel = nil
	}
	s.running = false
	s.mu.Unlock()
	if err := s.saveState(); err != nil && s.logger != nil {
		s.logger.Warn("token 刷新状态退出保存失败", "path", s.store.path, "error_type", fmt.Sprintf("%T", err))
	}
}

// run 从上一批结束时开始计算下一轮间隔，避免 ticker 积压。
func (s *TokenRefreshService) run(ctx context.Context) {
	now := time.Now()
	initialDelay := time.Duration(maxInt(s.app.settings.TokenRefreshInitialDelaySeconds, 0)) * time.Second
	s.mu.Lock()
	s.nextFastRunAt = now.Add(initialDelay).Unix()
	s.nextSlowRunAt = now.Add(initialDelay + time.Duration(s.app.settings.TokenRefreshSlowScanIntervalSeconds)*time.Second).Unix()
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.running = false
		s.phase = "stopped"
		s.mu.Unlock()
		_ = s.saveState()
	}()
	for {
		next := s.nextRunTime()
		if !sleepUntilContext(ctx, next) {
			return
		}
		s.runScheduledBatch(ctx, time.Now())
		if ctx.Err() != nil {
			return
		}
	}
}

// nextRunTime 返回快速和慢扫通道中较早的执行时间。
func (s *TokenRefreshService) nextRunTime() time.Time {
	s.mu.Lock()
	defer s.mu.Unlock()
	next := s.nextFastRunAt
	if next == 0 || (s.nextSlowRunAt > 0 && s.nextSlowRunAt < next) {
		next = s.nextSlowRunAt
	}
	return time.Unix(next, 0)
}

// sleepUntilContext 等待目标时间或立即响应取消。
func sleepUntilContext(ctx context.Context, target time.Time) bool {
	delay := time.Until(target)
	if delay <= 0 {
		return ctx.Err() == nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

// runScheduledBatch 按快速通道优先规则运行一个受预算约束的批次。
func (s *TokenRefreshService) runScheduledBatch(parent context.Context, now time.Time) {
	start := time.Now()
	settings := s.app.settings
	if !s.retryPendingAccounts() {
		s.finishBatch(now, start, tokenRefreshBatchSummary{}, false)
		return
	}
	accounts := s.app.accounts.Snapshot()
	snapshot := s.stateSnapshot()
	horizon := now.Add(time.Duration(maxInt(settings.TokenRefreshAheadSeconds, 0)) * time.Second).Unix()
	projected := projectedUsableAccounts(accounts, horizon)
	fastDue := s.fastDue(now)
	selected := []refreshCandidate{}
	skipped := 0
	fastPending := false
	fastBatch := false
	needsMediaRecovery := !hasUsableMediaAccount(accounts, now.Unix())
	if projected < maxInt(settings.AccountReadySetThreshold, 1) || needsMediaRecovery {
		var pending bool
		selected, skipped, pending = selectFastRefreshCandidates(accounts, snapshot, now, settings.TokenRefreshAheadSeconds, settings.TokenRefreshBatchSize, needsMediaRecovery)
		fastPending = pending
		fastBatch = fastDue && pending
		if !fastDue {
			selected = nil
		}
	}
	if len(selected) == 0 && !fastPending && s.slowDue(now) {
		candidate, slowSkipped, ok := selectSlowRefreshCandidate(accounts, snapshot, now, settings.AccountReadySetThreshold)
		skipped += slowSkipped
		if ok {
			selected = []refreshCandidate{candidate}
			s.updateSlowCursor(candidate.ID)
		}
	}
	s.mu.Lock()
	s.fastPending = fastPending
	s.lastRun = now.Unix()
	s.phase = "selecting"
	s.mu.Unlock()
	summary := tokenRefreshBatchSummary{Selected: len(selected), SkippedBackoff: skipped}
	if len(selected) == 0 || !s.breakerAllowsAttempt(now) {
		summary.BreakerState = s.breakerState()
		s.finishBatch(now, start, summary, fastBatch)
		return
	}
	if s.breakerState() == "half_open" && len(selected) > 1 {
		selected = selected[:1]
		summary.Selected = 1
	}
	batchCtx, cancel := context.WithTimeout(parent, time.Duration(settings.TokenRefreshBatchTimeoutSeconds)*time.Second)
	defer cancel()
	s.mu.Lock()
	s.phase = "browser_starting"
	s.mu.Unlock()
	if err := browserAutomationGate.lock(batchCtx, false); err != nil {
		summary.BreakerState = s.breakerState()
		s.finishBatch(now, start, summary, fastBatch)
		return
	}
	batch, err := s.browserFactory(s.logger)
	browserAutomationGate.Unlock()
	if err != nil {
		summary.Attempted = 1
		summary.Failed = 1
		s.recordFailure(selected[0].ID, classifyRefreshError(err), time.Now())
		summary.BreakerState = s.breakerState()
		s.finishBatch(now, start, summary, fastBatch)
		return
	}
	defer batch.Close()
	successes := make([]Account, 0, len(selected))
	for index, candidate := range selected {
		if batchCtx.Err() != nil {
			break
		}
		s.mu.Lock()
		s.phase = "refreshing"
		s.mu.Unlock()
		if err := browserAutomationGate.lock(batchCtx, false); err != nil {
			break
		}
		summary.Attempted++
		updated := candidate.Account
		err := batch.WithPage(batchCtx, candidate.Account, func(page pw.Page) error {
			var refreshErr error
			if candidate.CookieOnly {
				updated, refreshErr = s.refreshCookiesWithPage(batchCtx, page, candidate.Account)
			} else {
				updated, refreshErr = s.refreshWithPage(batchCtx, page, candidate.Account)
			}
			return refreshErr
		})
		browserAutomationGate.Unlock()
		if err != nil {
			errorClass := classifyRefreshError(err)
			summary.Failed++
			s.recordFailure(candidate.ID, errorClass, time.Now())
			if s.logger != nil {
				s.logger.Debug("token 刷新账号失败", "account_hash", candidate.ID[:12], "error_class", errorClass)
			}
		} else {
			summary.Succeeded++
			successes = append(successes, updated)
			s.recordSuccess(candidate.ID, time.Now())
		}
		if s.breakerState() == "open" {
			break
		}
		if index+1 < len(selected) {
			sleepWithContext(batchCtx, time.Duration(maxInt(settings.TokenRefreshStaggerMS, 0))*time.Millisecond)
		}
	}
	if len(successes) > 0 {
		if err := s.app.accounts.ApplyRefreshResults(successes); err != nil {
			s.mu.Lock()
			s.pendingAccountResults = append([]Account(nil), successes...)
			s.lastError = err.Error()
			s.phase = "error"
			s.mu.Unlock()
			if s.logger != nil {
				s.logger.Warn("token 刷新账号批量保存失败", "path", s.app.settings.AccountsFile, "error_type", fmt.Sprintf("%T", err), "count", len(successes))
			}
		} else {
			s.mu.Lock()
			s.lastRefresh = time.Now().Unix()
			s.mu.Unlock()
		}
	}
	summary.BreakerState = s.breakerState()
	s.finishBatch(now, start, summary, fastBatch)
}

// retryPendingAccounts 在开始新刷新前重试上轮失败的账号批量保存。
func (s *TokenRefreshService) retryPendingAccounts() bool {
	s.mu.Lock()
	pending := append([]Account(nil), s.pendingAccountResults...)
	s.mu.Unlock()
	if len(pending) == 0 {
		return true
	}
	if err := s.app.accounts.ApplyRefreshResults(pending); err != nil {
		s.mu.Lock()
		s.lastError = err.Error()
		s.phase = "error"
		s.mu.Unlock()
		return false
	}
	s.mu.Lock()
	s.pendingAccountResults = nil
	s.lastError = ""
	s.mu.Unlock()
	return true
}

// finishBatch 更新聚合指标、保存状态，并从批次结束时安排下一轮。
func (s *TokenRefreshService) finishBatch(now, start time.Time, summary tokenRefreshBatchSummary, fastBatch bool) {
	summary.Duration = time.Since(start)
	finished := time.Now()
	s.mu.Lock()
	s.phase = "idle"
	s.attemptedTotal += summary.Attempted
	s.succeededTotal += summary.Succeeded
	s.failedTotal += summary.Failed
	s.lastBatchDurationMS = summary.Duration.Milliseconds()
	s.lastBatchSelected = summary.Selected
	if fastBatch || s.nextFastRunAt <= now.Unix() {
		s.nextFastRunAt = finished.Add(time.Duration(s.app.settings.TokenRefreshCheckInterval) * time.Second).Unix()
	}
	if !fastBatch && s.nextSlowRunAt <= now.Unix() {
		s.nextSlowRunAt = finished.Add(time.Duration(s.app.settings.TokenRefreshSlowScanIntervalSeconds) * time.Second).Unix()
	}
	s.mu.Unlock()
	if err := s.saveState(); err != nil {
		s.mu.Lock()
		s.phase = "error"
		s.lastError = err.Error()
		s.mu.Unlock()
	}
	if s.logger != nil {
		s.logger.Info("token 刷新批次完成",
			"selected", summary.Selected,
			"attempted", summary.Attempted,
			"succeeded", summary.Succeeded,
			"failed", summary.Failed,
			"skipped_backoff", summary.SkippedBackoff,
			"duration_ms", summary.Duration.Milliseconds(),
			"breaker_state", s.breakerState(),
		)
	}
}

// recordFailure 更新身份退避，并按错误类别推进全局熔断器。
func (s *TokenRefreshService) recordFailure(id, errorClass string, now time.Time) {
	s.mu.Lock()
	accountState := s.state.Accounts[id]
	accountState.Failures++
	accountState.LastAttemptAt = now.Unix()
	accountState.LastErrorClass = errorClass
	jitter := rand.Float64()*0.2 - 0.1
	accountState.NextAttemptAt = now.Add(refreshBackoffDuration(accountState.Failures, jitter)).Unix()
	s.state.Accounts[id] = accountState
	breakerChanged := s.advanceBreakerFailureLocked(errorClass, now)
	s.mu.Unlock()
	if breakerChanged {
		_ = s.saveState()
	}
}

// recordSuccess 清除身份退避并关闭半开或已有失败计数的熔断器。
func (s *TokenRefreshService) recordSuccess(id string, now time.Time) {
	s.mu.Lock()
	delete(s.state.Accounts, id)
	changed := s.state.Breaker.State != "closed" || s.state.Breaker.ConsecutiveFailures != 0
	s.state.Breaker = refreshBreakerState{State: "closed"}
	s.mu.Unlock()
	if changed {
		_ = s.saveState()
	}
}

// advanceBreakerFailureLocked 记录同类连续失败并在阈值处打开熔断。
func (s *TokenRefreshService) advanceBreakerFailureLocked(errorClass string, now time.Time) bool {
	breaker := &s.state.Breaker
	if breaker.State == "half_open" {
		breaker.State = "open"
		breaker.ErrorClass = errorClass
		breaker.ConsecutiveFailures = s.app.settings.TokenRefreshBreakerFailures
		breaker.OpenUntil = now.Add(time.Duration(s.app.settings.TokenRefreshBreakerCooldownSeconds) * time.Second).Unix()
		return true
	}
	if breaker.ErrorClass == errorClass {
		breaker.ConsecutiveFailures++
	} else {
		breaker.ErrorClass = errorClass
		breaker.ConsecutiveFailures = 1
	}
	if breaker.ConsecutiveFailures >= s.app.settings.TokenRefreshBreakerFailures {
		breaker.State = "open"
		breaker.OpenUntil = now.Add(time.Duration(s.app.settings.TokenRefreshBreakerCooldownSeconds) * time.Second).Unix()
		return true
	}
	return false
}

// breakerAllowsAttempt 在冷却结束后只允许一个半开试探批次。
func (s *TokenRefreshService) breakerAllowsAttempt(now time.Time) bool {
	s.mu.Lock()
	changed := false
	allowed := true
	if s.state.Breaker.State == "open" {
		if now.Unix() < s.state.Breaker.OpenUntil {
			allowed = false
		} else {
			s.state.Breaker.State = "half_open"
			changed = true
		}
	}
	s.mu.Unlock()
	if changed {
		_ = s.saveState()
	}
	return allowed
}

// updateSlowCursor 持久化慢扫游标，不记录原始账号值。
func (s *TokenRefreshService) updateSlowCursor(id string) {
	s.mu.Lock()
	s.state.SlowScanCursor = id
	s.mu.Unlock()
}

// stateSnapshot 返回可安全用于候选计算的刷新状态副本。
func (s *TokenRefreshService) stateSnapshot() refreshPersistentState {
	s.mu.Lock()
	defer s.mu.Unlock()
	return cloneRefreshState(s.state)
}

// cloneRefreshState 深拷贝账号退避表。
func cloneRefreshState(state refreshPersistentState) refreshPersistentState {
	copyState := state
	copyState.Accounts = make(map[string]refreshAccountState, len(state.Accounts))
	for id, accountState := range state.Accounts {
		copyState.Accounts[id] = accountState
	}
	return copyState
}

// saveState 保存当前刷新状态快照。
func (s *TokenRefreshService) saveState() error {
	return s.store.Save(s.stateSnapshot())
}

// fastDue 判断快速通道是否到期。
func (s *TokenRefreshService) fastDue(now time.Time) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.nextFastRunAt == 0 || s.nextFastRunAt <= now.Unix()
}

// slowDue 判断慢扫通道是否到期。
func (s *TokenRefreshService) slowDue(now time.Time) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.nextSlowRunAt > 0 && s.nextSlowRunAt <= now.Unix()
}

// breakerState 返回当前熔断状态。
func (s *TokenRefreshService) breakerState() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state.Breaker.State
}

// ReadyStateLoaded 表示独立刷新状态已完成加载或初始化。
func (s *TokenRefreshService) ReadyStateLoaded() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.loaded
}

// Status 返回管理面板和就绪探针所需的聚合状态。
func (s *TokenRefreshService) Status() map[string]any {
	if s == nil {
		return map[string]any{"running": false, "phase": "unavailable", "breaker_state": "closed"}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	backoffCount := 0
	now := time.Now().Unix()
	for _, accountState := range s.state.Accounts {
		if accountState.NextAttemptAt > now {
			backoffCount++
		}
	}
	return map[string]any{
		"running":                  s.running,
		"enabled":                  s.app.settings.TokenRefreshEnabled,
		"loaded":                   s.loaded,
		"check_interval":           s.app.settings.TokenRefreshCheckInterval,
		"ahead_seconds":            s.app.settings.TokenRefreshAheadSeconds,
		"phase":                    s.phase,
		"fast_pending":             s.fastPending,
		"slow_scan_cursor_present": s.state.SlowScanCursor != "",
		"last_run":                 s.lastRun,
		"last_refresh":             s.lastRefresh,
		"attempted_total":          s.attemptedTotal,
		"succeeded_total":          s.succeededTotal,
		"refreshed_total":          s.succeededTotal,
		"failed_total":             s.failedTotal,
		"backoff_count":            backoffCount,
		"breaker_state":            s.state.Breaker.State,
		"breaker_open_until":       s.state.Breaker.OpenUntil,
		"last_batch_duration_ms":   s.lastBatchDurationMS,
		"last_batch_selected":      s.lastBatchSelected,
		"next_fast_run_at":         s.nextFastRunAt,
		"next_slow_run_at":         s.nextSlowRunAt,
		"last_error":               s.lastError,
	}
}

// runTokenRefreshSelfTest 在隔离临时状态中验证退避、熔断和半开恢复。
func runTokenRefreshSelfTest() (map[string]any, error) {
	tmp, err := os.CreateTemp("", "qwen2api-refresh-self-test-*.json")
	if err != nil {
		return nil, err
	}
	path := tmp.Name()
	if err := tmp.Close(); err != nil {
		return nil, err
	}
	_ = os.Remove(path)
	defer os.Remove(path)
	service := &TokenRefreshService{
		app: &App{settings: Settings{
			TokenRefreshBreakerFailures:        5,
			TokenRefreshBreakerCooldownSeconds: 21600,
		}},
		store: &tokenRefreshStateStore{path: path},
		state: newTokenRefreshState(),
	}
	now := time.Now()
	for index := 0; index < 5; index++ {
		service.recordFailure(fmt.Sprintf("%064d", index), "network", now)
	}
	openObserved := service.breakerState() == "open"
	service.mu.Lock()
	service.state.Breaker.OpenUntil = now.Add(-time.Second).Unix()
	service.mu.Unlock()
	halfOpenObserved := service.breakerAllowsAttempt(now) && service.breakerState() == "half_open"
	service.recordSuccess(strings.Repeat("f", 64), now)
	closedObserved := service.breakerState() == "closed"
	if !openObserved || !halfOpenObserved || !closedObserved {
		return nil, errors.New("breaker lifecycle assertion failed")
	}
	return map[string]any{
		"status":               "ok",
		"attempted":            5,
		"failed":               5,
		"backoff_count":        5,
		"breaker_open":         openObserved,
		"half_open_probe":      halfOpenObserved,
		"breaker_recovered":    closedObserved,
		"error_class":          "network",
		"state_schema_version": refreshStateVersion,
	}, nil
}
