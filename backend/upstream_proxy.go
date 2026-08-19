package main

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	pw "github.com/playwright-community/playwright-go"
)

const proxyUUIDPlaceholder = "{uuid}"

var proxyUUIDPattern = regexp.MustCompile(`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

// UpstreamProxyPool 为账号稳定选择出口，并复用每个出口的 HTTP 连接池。
type UpstreamProxyPool struct {
	template string
	uuids    []string

	mu      sync.Mutex
	clients map[int]*http.Client
}

// loadUpstreamProxyPool 从权限受控文件加载代理模板和出口 UUID。
func loadUpstreamProxyPool(templateFile, uuidsFile string) (*UpstreamProxyPool, error) {
	templateFile = strings.TrimSpace(templateFile)
	uuidsFile = strings.TrimSpace(uuidsFile)
	if templateFile == "" && uuidsFile == "" {
		return nil, nil
	}
	if templateFile == "" || uuidsFile == "" {
		return nil, errors.New("upstream proxy requires both template and UUID files")
	}
	templateRaw, err := os.ReadFile(templateFile)
	if err != nil {
		return nil, fmt.Errorf("read upstream proxy template: %w", err)
	}
	templateText := strings.TrimSpace(string(templateRaw))
	if strings.Count(templateText, proxyUUIDPlaceholder) != 1 {
		return nil, errors.New("upstream proxy template must contain exactly one {uuid} placeholder")
	}
	validationURL := strings.Replace(templateText, proxyUUIDPlaceholder, "00000000-0000-4000-8000-000000000000", 1)
	parsed, err := url.Parse(validationURL)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil, errors.New("upstream proxy template must be a valid HTTP(S) URL")
	}
	uuidRaw, err := os.ReadFile(uuidsFile)
	if err != nil {
		return nil, fmt.Errorf("read upstream proxy UUIDs: %w", err)
	}
	seen := map[string]struct{}{}
	uuids := make([]string, 0)
	for _, raw := range strings.FieldsFunc(string(uuidRaw), func(r rune) bool { return r == '\n' || r == '\r' || r == ',' }) {
		value := strings.ToLower(strings.TrimSpace(raw))
		if value == "" {
			continue
		}
		if !proxyUUIDPattern.MatchString(value) {
			return nil, errors.New("upstream proxy UUID file contains an invalid entry")
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		uuids = append(uuids, value)
	}
	if len(uuids) == 0 {
		return nil, errors.New("upstream proxy UUID file is empty")
	}
	return &UpstreamProxyPool{template: templateText, uuids: uuids, clients: map[int]*http.Client{}}, nil
}

// enabled 表示已加载至少一个可用出口。
func (p *UpstreamProxyPool) enabled() bool {
	return p != nil && p.template != "" && len(p.uuids) > 0
}

// indexForIdentity 使用账号规范化标识做稳定映射，避免同一会话中途切换出口。
func (p *UpstreamProxyPool) indexForIdentity(identity QwenRequestIdentity) int {
	key := strings.ToLower(strings.TrimSpace(identity.Email))
	if key == "" {
		key = strings.TrimSpace(identity.Token)
	}
	sum := sha256.Sum256([]byte(key))
	value := uint64(0)
	for _, b := range sum[:8] {
		value = value<<8 | uint64(b)
	}
	return int(value % uint64(len(p.uuids)))
}

// proxyURL 返回身份对应的代理地址；返回值仅传给网络栈，不写日志。
func (p *UpstreamProxyPool) proxyURL(identity QwenRequestIdentity) (*url.URL, int, bool) {
	if !p.enabled() {
		return nil, 0, false
	}
	index := p.indexForIdentity(identity)
	raw := strings.Replace(p.template, proxyUUIDPlaceholder, p.uuids[index], 1)
	parsed, err := url.Parse(raw)
	if err != nil {
		return nil, 0, false
	}
	return parsed, index, true
}

// exitLabel 仅返回不可逆的出口摘要，供聚合诊断使用。
func (p *UpstreamProxyPool) exitLabel(identity QwenRequestIdentity) string {
	_, index, ok := p.proxyURL(identity)
	if !ok {
		return "direct"
	}
	sum := sha256.Sum256([]byte(p.uuids[index]))
	return fmt.Sprintf("proxy-%x", sum[:6])
}

// httpClientForIdentity 返回身份固定出口对应的 HTTP 客户端。
func (p *UpstreamProxyPool) httpClientForIdentity(identity QwenRequestIdentity, settings Settings) *http.Client {
	proxyURL, index, ok := p.proxyURL(identity)
	if !ok {
		return nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if client := p.clients[index]; client != nil {
		return client
	}
	client := newQwenHTTPClient(settings, http.ProxyURL(proxyURL))
	p.clients[index] = client
	return client
}

// playwrightProxyForIdentity 将 URL 凭据拆分为 Playwright 的代理参数。
func (p *UpstreamProxyPool) playwrightProxyForIdentity(identity QwenRequestIdentity) *pw.Proxy {
	proxyURL, _, ok := p.proxyURL(identity)
	if !ok {
		return nil
	}
	proxy := &pw.Proxy{Server: proxyURL.Scheme + "://" + proxyURL.Host}
	if proxyURL.User != nil {
		if username := proxyURL.User.Username(); username != "" {
			proxy.Username = pw.String(username)
		}
		if password, present := proxyURL.User.Password(); present {
			proxy.Password = pw.String(password)
		}
	}
	return proxy
}

// newQwenHTTPClient 构建一致的直连或代理 HTTP 客户端。
func newQwenHTTPClient(settings Settings, proxy func(*http.Request) (*url.URL, error)) *http.Client {
	return &http.Client{
		Transport: &http.Transport{
			Proxy:                 proxy,
			MaxIdleConns:          100,
			MaxIdleConnsPerHost:   20,
			IdleConnTimeout:       30 * time.Second,
			ResponseHeaderTimeout: streamTimeoutDuration(settings.UpstreamStreamHeaderTimeoutSeconds),
			ForceAttemptHTTP2:     true,
		},
		Timeout: 5 * time.Minute,
	}
}
