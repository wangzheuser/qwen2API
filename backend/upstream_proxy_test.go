package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestLoadUpstreamProxyPoolAndStableIdentityMapping 验证配置加载、账号粘性和凭据拆分。
func TestLoadUpstreamProxyPoolAndStableIdentityMapping(t *testing.T) {
	dir := t.TempDir()
	templateFile := filepath.Join(dir, "template")
	uuidsFile := filepath.Join(dir, "uuids")
	if err := os.WriteFile(templateFile, []byte("http://node.{uuid}:fixture@127.0.0.1:9200\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(uuidsFile, []byte("11111111-1111-4111-8111-111111111111\n22222222-2222-4222-8222-222222222222\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	pool, err := loadUpstreamProxyPool(templateFile, uuidsFile)
	if err != nil {
		t.Fatal(err)
	}
	first := QwenRequestIdentity{Email: " User@Example.com ", Token: "old-token"}
	second := QwenRequestIdentity{Email: "user@example.com", Token: "new-token"}
	firstURL, firstIndex, ok := pool.proxyURL(first)
	if !ok {
		t.Fatal("expected proxy URL")
	}
	secondURL, secondIndex, ok := pool.proxyURL(second)
	if !ok || firstIndex != secondIndex || firstURL.String() != secondURL.String() {
		t.Fatal("same normalized account must keep a stable exit")
	}
	if strings.Contains(pool.exitLabel(first), pool.uuids[firstIndex]) {
		t.Fatal("exit label must not expose raw UUID")
	}
	playwrightProxy := pool.playwrightProxyForIdentity(first)
	if playwrightProxy == nil || playwrightProxy.Username == nil || !strings.Contains(*playwrightProxy.Username, pool.uuids[firstIndex]) {
		t.Fatal("playwright proxy must use the selected UUID username")
	}
	if playwrightProxy.Password == nil || *playwrightProxy.Password != "fixture" {
		t.Fatal("playwright proxy password mismatch")
	}
	client := pool.httpClientForIdentity(first, Settings{})
	transport, ok := client.Transport.(*http.Transport)
	if !ok || transport.Proxy == nil {
		t.Fatal("expected proxy-aware HTTP transport")
	}
	request, _ := http.NewRequest(http.MethodGet, qwenBaseURL, nil)
	actualURL, err := transport.Proxy(request)
	if err != nil || actualURL.String() != firstURL.String() {
		t.Fatal("HTTP and Playwright must use the same selected exit")
	}
}

// TestLoadUpstreamProxyPoolRejectsPartialOrInvalidConfiguration 验证配置缺失时直接阻止带外回退。
func TestLoadUpstreamProxyPoolRejectsPartialOrInvalidConfiguration(t *testing.T) {
	dir := t.TempDir()
	templateFile := filepath.Join(dir, "template")
	uuidsFile := filepath.Join(dir, "uuids")
	if _, err := loadUpstreamProxyPool(templateFile, ""); err == nil {
		t.Fatal("partial proxy configuration must fail")
	}
	if err := os.WriteFile(templateFile, []byte("http://node.{uuid}:secret@127.0.0.1:9200"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(uuidsFile, []byte("not-a-uuid\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadUpstreamProxyPool(templateFile, uuidsFile); err == nil {
		t.Fatal("invalid UUID must fail")
	}
}
