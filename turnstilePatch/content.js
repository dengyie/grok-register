// Turnstile Patch - 隐藏自动化标识，加速 Turnstile 验证
// 在 document_start 阶段执行，确保在页面脚本之前生效

(function () {
    "use strict";

    // 1. 隐藏 navigator.webdriver 标识
    // Chrome 自动化模式下 navigator.webdriver = true，Turnstile 会检测此属性
    try {
        Object.defineProperty(navigator, "webdriver", {
            get: function () {
                return false;
            },
            configurable: true,
        });
    } catch (e) {}

    // 2. 移除 Chrome 自动化相关的 Runtime 属性
    try {
        if (window.chrome && window.chrome.runtime) {
            delete window.chrome.runtime.onConnect;
            delete window.chrome.runtime.onMessage;
        }
    } catch (e) {}

    // 3. 覆盖 permissions.query，隐藏 notifications 权限异常
    try {
        var origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = function (params) {
            if (params.name === "notifications") {
                return Promise.resolve({ state: Notification.permission });
            }
            return origQuery(params);
        };
    } catch (e) {}

    // 4. 修补 plugin 数量，模拟正常浏览器
    try {
        Object.defineProperty(navigator, "plugins", {
            get: function () {
                return [1, 2, 3, 4, 5];
            },
            configurable: true,
        });
    } catch (e) {}

    // 5. 修补 languages 属性
    try {
        Object.defineProperty(navigator, "languages", {
            get: function () {
                return ["en-US", "en"];
            },
            configurable: true,
        });
    } catch (e) {}

    // 6. 页面加载完成后，自动监控并点击 Turnstile 复选框
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", autoClickTurnstile);
    } else {
        autoClickTurnstile();
    }

    function autoClickTurnstile() {
        // 定时检查 Turnstile iframe 是否出现
        var checkCount = 0;
        var maxChecks = 100; // 最多检查 100 次（约 50 秒）
        var timer = setInterval(function () {
            checkCount++;
            if (checkCount > maxChecks) {
                clearInterval(timer);
                return;
            }
            try {
                // 查找 Turnstile iframe
                var iframes = document.querySelectorAll(
                    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
                );
                for (var i = 0; i < iframes.length; i++) {
                    var iframe = iframes[i];
                    try {
                        // 尝试访问 iframe 内部的 checkbox
                        var body = iframe.contentDocument || iframe.contentWindow.document;
                        var checkbox = body.querySelector(
                            'input[type="checkbox"], .mark, #cf-chl-widget-nomu1_resp'
                        );
                        if (checkbox && !checkbox.checked) {
                            checkbox.click();
                        }
                    } catch (e) {
                        // 跨域限制，尝试通过 postMessage 触发
                        try {
                            iframe.contentWindow.postMessage(
                                { type: "turnstile-auto-click" },
                                "*"
                            );
                        } catch (e2) {}
                    }
                }

                // 也尝试直接操作 Turnstile API
                if (
                    window.turnstile &&
                    typeof window.turnstile.getResponse === "function"
                ) {
                    var resp = window.turnstile.getResponse();
                    if (resp && resp.length > 0) {
                        clearInterval(timer); // 已获得 token，停止检查
                    }
                }
            } catch (e) {}
        }, 500);
    }
})();
