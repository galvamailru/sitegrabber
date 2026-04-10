/**
 * Витрина каталога: дизайн и «печать» ответа как в aichatbot (CodingNepal),
 * источник ответа — POST /api/catalog-chat (JSON), typewriter как при SSE.
 */
(function () {
  const chatBody = document.querySelector(".chat-body");
  const messageInput = document.querySelector(".message-input");
  const chatForm = document.querySelector(".chat-form");
  const sendBtn = document.querySelector(".send-btn");
  const chatbotToggler = document.getElementById("chatbot-toggler");
  const closeChatbot = document.getElementById("close-chatbot");
  const errorEl = document.querySelector(".error-msg");

  if (!chatBody || !messageInput || !chatForm || !sendBtn || !chatbotToggler || !closeChatbot || !errorEl) {
    return;
  }

  var isFullLayout = (function () {
    var params = new URLSearchParams(window.location.search);
    var full = params.get("layout") === "full";
    if (full) {
      document.body.classList.add("layout-full", "show-chatbot");
    }
    return full;
  })();

  var layoutSwitch = document.getElementById("layout-switch");
  if (layoutSwitch) {
    layoutSwitch.textContent = isFullLayout ? "Всплывающее окно" : "Полноэкранный";
    var basePath = window.location.pathname || "/";
    var q = new URLSearchParams(window.location.search);
    if (isFullLayout) {
      q.delete("layout");
    } else {
      q.set("layout", "full");
    }
    var qs = q.toString();
    layoutSwitch.href = basePath.split("?")[0] + (qs ? "?" + qs : "");
    layoutSwitch.addEventListener("click", function (e) {
      e.preventDefault();
      window.location.href = layoutSwitch.href;
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function toDisplayableError(detail, fallback) {
    if (detail == null) return fallback || "Ошибка запроса";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0 && detail[0].msg) return detail[0].msg;
    if (typeof detail === "object" && detail.msg) return detail.msg;
    return fallback || "Ошибка запроса";
  }

  function formatBold(text) {
    if (!text) return "";
    var escaped = escapeHtml(text);
    return escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  }

  function createMessageElement(content, role) {
    const div = document.createElement("div");
    div.className = "message " + (role === "user" ? "user-message" : "bot-message");
    const text = document.createElement("div");
    text.className = "message-text";
    if (role === "bot") {
      text.innerHTML = formatBold(content);
    } else {
      text.textContent = content;
    }
    div.appendChild(text);
    return { wrap: div, text };
  }

  var MAX_MESSAGE_LENGTH = 1000;
  var MAX_LENGTH_MSG = "Размер сообщения ограничен 1000 символами.";
  var MAX_LENGTH_BOT_REPLY =
    "Сообщение превышает допустимый размер (1000 символов). Сократите текст и отправьте снова.";

  function runTypewriter(botEl, fullText) {
    var streamedText = "";
    var pendingBuffer = fullText || "";
    var streamEnded = true;
    var typewriterMs = 18;
    var typewriterCharsPerTick = 2;

    function drainTypewriter() {
      if (pendingBuffer.length === 0) {
        if (streamEnded) clearInterval(typewriterInterval);
        return;
      }
      var take = Math.min(typewriterCharsPerTick, pendingBuffer.length);
      streamedText += pendingBuffer.slice(0, take);
      pendingBuffer = pendingBuffer.slice(take);
      botEl.text.innerHTML = formatBold(streamedText);
      chatBody.scrollTo({ top: chatBody.scrollHeight, behavior: "smooth" });
    }

    var typewriterInterval = setInterval(drainTypewriter, typewriterMs);
    if (pendingBuffer.length === 0) clearInterval(typewriterInterval);
    return typewriterInterval;
  }

  async function handleSend(e) {
    e.preventDefault();
    const text = messageInput.value.trim();
    if (!text) return;

    if (!window.__SITE_PROJECT_ID__) {
      errorEl.textContent = "Чат недоступен: не задан проект.";
      return;
    }

    if (text.length > MAX_MESSAGE_LENGTH) {
      messageInput.value = "";
      errorEl.textContent = "";
      const botEl = createMessageElement(MAX_LENGTH_BOT_REPLY, "bot");
      chatBody.appendChild(botEl.wrap);
      chatBody.scrollTo({ top: chatBody.scrollHeight, behavior: "smooth" });
      return;
    }

    messageInput.value = "";
    errorEl.textContent = "";
    const userEl = createMessageElement(text, "user");
    chatBody.appendChild(userEl.wrap);
    chatBody.scrollTo({ top: chatBody.scrollHeight, behavior: "smooth" });

    const botEl = createMessageElement("", "bot");
    botEl.wrap.classList.add("thinking");
    botEl.text.innerHTML =
      '<div class="thinking-indicator"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>';
    chatBody.appendChild(botEl.wrap);
    chatBody.scrollTo({ top: chatBody.scrollHeight, behavior: "smooth" });

    sendBtn.disabled = true;
    var typewriterInterval;

    try {
      const res = await fetch("/api/catalog-chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          site_project_id: window.__SITE_PROJECT_ID__,
          question: text,
        }),
      });

      botEl.wrap.classList.remove("thinking");
      botEl.text.innerHTML = "";
      botEl.text.textContent = "";

      if (!res.ok) {
        const err = await res.json().catch(function () {
          return { detail: res.statusText };
        });
        var errMsg = res.status === 422 ? MAX_LENGTH_BOT_REPLY : toDisplayableError(err.detail);
        botEl.text.textContent = errMsg;
        errorEl.textContent = res.status === 422 ? MAX_LENGTH_MSG : errMsg;
        sendBtn.disabled = false;
        return;
      }

      const data = await res.json().catch(function () {
        return {};
      });
      var answer = data.answer != null ? String(data.answer) : "";
      if (!answer.trim()) {
        botEl.text.textContent = "Нет ответа";
        return;
      }

      typewriterInterval = runTypewriter(botEl, answer);
    } catch (err) {
      botEl.wrap.classList.remove("thinking");
      botEl.text.textContent = "Ошибка: " + (err.message || "сеть");
      errorEl.textContent = err.message || "Ошибка запроса";
      if (typeof typewriterInterval !== "undefined") clearInterval(typewriterInterval);
    } finally {
      sendBtn.disabled = false;
      chatBody.scrollTo({ top: chatBody.scrollHeight, behavior: "smooth" });
    }
  }

  chatForm.addEventListener("submit", handleSend);
  closeChatbot.addEventListener("click", function () {
    document.body.classList.remove("show-chatbot");
  });
  chatbotToggler.addEventListener("click", function () {
    document.body.classList.toggle("show-chatbot");
  });

  messageInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend(e);
    }
  });

  function checkLengthWarning() {
    if (messageInput.value.length >= MAX_MESSAGE_LENGTH) {
      errorEl.textContent = MAX_LENGTH_MSG;
    } else if (errorEl.textContent === MAX_LENGTH_MSG) {
      errorEl.textContent = "";
    }
  }

  messageInput.addEventListener("input", function () {
    setTimeout(checkLengthWarning, 0);
  });

  messageInput.addEventListener("paste", function () {
    setTimeout(checkLengthWarning, 0);
    setTimeout(checkLengthWarning, 50);
  });

  var welcomeText =
    "Здравствуйте! Я консультант каталога. Задайте вопрос по товарам, характеристикам или оформлению — подскажу по данным сайта.";
  var welcomeDelayMs = 800;
  setTimeout(function () {
    var welcome = createMessageElement(welcomeText, "bot");
    chatBody.appendChild(welcome.wrap);
  }, welcomeDelayMs);
})();
