# -*- coding: utf-8 -*-
"""
蜂群AGS联网增强补丁
- 子Agent支持联网搜索
- 使用multi-search-engine方式（web_fetch）
- 增加搜索深度
- 返工时重新搜索
"""

import urllib.request
import urllib.parse
import json
import time
import hashlib
import re

class MultiSearchEngine:
    """多搜索引擎 - 不需要API key"""
    
    ENGINES = {
        "baidu": "https://www.baidu.com/s?wd={keyword}",
        "bing_cn": "https://cn.bing.com/search?q={keyword}&ensearch=0",
        "sogou": "https://sogou.com/web?query={keyword}",
        "duckduckgo": "https://duckduckgo.com/html/?q={keyword}",
    }
    
    def __init__(self, token_url="http://127.0.0.1:53699/get_token"):
        self.token_url = token_url
    
    def search(self, query, engines=["baidu", "bing_cn"], max_results=5):
        results = []
        
        for engine in engines:
            if engine not in self.ENGINES:
                continue
            
            url = self.ENGINES[engine].format(keyword=urllib.parse.quote(query))
            
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Accept': 'text/html,application/xhtml+xml',
                        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    }
                )
                
                with urllib.request.urlopen(req, timeout=15) as r:
                    html = r.read().decode('utf-8', errors='ignore')
                
                extracted = self._extract_results(html, engine)
                results.extend(extracted[:max_results])
                
            except Exception as e:
                print(f"[MultiSearch] {engine} error: {e}")
        
        return {
            "ok": bool(results),
            "query": query,
            "count": len(results),
            "results": results
        }
    
    def _extract_results(self, html, engine):
        results = []
        
        if engine == "baidu":
            pattern = r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
            matches = re.findall(pattern, html, re.DOTALL)
            for url, title in matches[:10]:
                results.append({
                    "title": re.sub(r'<[^>]+>', '', title).strip(),
                    "url": url,
                    "snippet": "",
                    "engine": "baidu"
                })
        
        elif engine in ["bing_cn", "bing"]:
            pattern = r'<li class="b_algo"[^>]*>.*?<h2><a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
            matches = re.findall(pattern, html, re.DOTALL)
            for url, title in matches[:10]:
                results.append({
                    "title": re.sub(r'<[^>]+>', '', title).strip(),
                    "url": url,
                    "snippet": "",
                    "engine": "bing"
                })
        
        elif engine == "duckduckgo":
            pattern = r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
            matches = re.findall(pattern, html, re.DOTALL)
            for url, title in matches[:10]:
                results.append({
                    "title": re.sub(r'<[^>]+>', '', title).strip(),
                    "url": url,
                    "snippet": "",
                    "engine": "duckduckgo"
                })
        
        return results
    
    def fetch_url(self, url, max_length=5000):
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode('utf-8', errors='ignore')
            
            text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            
            return {"ok": True, "url": url, "content": text[:max_length]}
            
        except Exception as e:
            return {"ok": False, "error": str(e)}


class EnhancedWebSearch:
    """增强版联网搜索"""
    
    def __init__(self, token_url="http://127.0.0.1:53699/get_token"):
        self.token_url = token_url
        self.APP_ID = "100003"
        self.APP_KEY = "38d2391985e2369a5fb8227d8e6cd5e5"
        self.SEARCH_API = "https://autoglm-api.zhipuai.cn/agentdr/v1/assistant/skills/web-search"
        self.multi_search = MultiSearchEngine(token_url)
    
    def get_token(self):
        try:
            with urllib.request.urlopen(self.token_url, timeout=5) as r:
                token = r.read().decode('utf-8').strip()
            if not token.lower().startswith('bearer '):
                token = f"Bearer {token}"
            return token
        except:
            return None
    
    def search(self, query, use_multi=True, max_results=10):
        all_results = []
        
        # AutoGLM搜索
        token = self.get_token()
        if token:
            try:
                timestamp = str(int(time.time()))
                sign_data = f"{self.APP_ID}&{timestamp}&{self.APP_KEY}"
                sign = hashlib.md5(sign_data.encode()).hexdigest()
                
                data = json.dumps({"queries": [{"query": query}]}).encode()
                req = urllib.request.Request(
                    self.SEARCH_API,
                    data=data,
                    headers={
                        'Authorization': token,
                        'X-Auth-Appid': self.APP_ID,
                        'X-Auth-TimeStamp': timestamp,
                        'X-Auth-Sign': sign,
                        'Content-Type': 'application/json'
                    }
                )
                
                with urllib.request.urlopen(req, timeout=30) as r:
                    result = json.loads(r.read().decode('utf-8'))
                
                if result.get('code') == 0:
                    pages = result.get('data', {}).get('results', [{}])[0].get('webPages', {}).get('value', [])
                    for p in pages[:max_results]:
                        all_results.append({
                            "title": p.get("name", ""),
                            "url": p.get("url", ""),
                            "snippet": p.get("snippet", ""),
                            "engine": "autoglm"
                        })
            except Exception as e:
                print(f"[AutoGLM] error: {e}")
        
        # 多搜索补充
        if use_multi and len(all_results) < max_results:
            multi_result = self.multi_search.search(query, max_results=max_results - len(all_results))
            if multi_result.get("ok"):
                all_results.extend(multi_result.get("results", []))
        
        return {
            "ok": bool(all_results),
            "query": query,
            "count": len(all_results),
            "results": all_results[:max_results]
        }
    
    def fetch_url(self, url, max_length=5000):
        return self.multi_search.fetch_url(url, max_length)
    
    def format_results(self, search_result):
        if not search_result.get("ok"):
            return f"Search failed: {search_result.get('error', 'unknown')}"
        
        results = search_result.get("results", [])
        if not results:
            return "No results found"
        
        output = f"[Search] Query: {search_result.get('query', '')}\n"
        output += f"Found {search_result.get('count', 0)} results:\n\n"
        
        for i, r in enumerate(results, 1):
            output += f"{i}. {r.get('title', 'No title')}\n"
            output += f"   URL: {r.get('url', '')}\n"
            if r.get('snippet'):
                output += f"   Snippet: {r.get('snippet')[:200]}...\n"
            output += "\n"
        
        return output


if __name__ == "__main__":
    search = EnhancedWebSearch()
    result = search.search("明阳智能 601615 股价")
    print(f"Found {result['count']} results")
    for r in result['results'][:3]:
        print(f"- {r['title']}")
