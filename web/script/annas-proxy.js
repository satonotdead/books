(function () {
  "use strict";

  const MD5_REGEX = /\/md5\/([A-Fa-f0-9]{32})(?=$|[/?#])/;
  let subdirectories = [];

  function injectStyles() {
    if (document.getElementById("stacks-annas-proxy-styles")) return;

    const style = document.createElement("style");
    style.id = "stacks-annas-proxy-styles";
    style.textContent = `
      #stacks-annas-toolbar {
        position: sticky;
        top: 0;
        z-index: 9999;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 12px 18px;
        background: rgba(10, 16, 24, 0.96);
        color: #f8fafc;
        border-bottom: 1px solid rgba(148, 163, 184, 0.28);
        backdrop-filter: blur(10px);
      }

      #stacks-annas-toolbar a {
        color: #f8fafc;
        text-decoration: none;
      }

      #stacks-annas-toolbar .stacks-annas-toolbar__brand {
        display: inline-flex;
        align-items: center;
        gap: 10px;
        font-weight: 700;
        letter-spacing: 0.02em;
      }

      #stacks-annas-toolbar .stacks-annas-toolbar__links {
        display: inline-flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
      }

      #stacks-annas-toolbar .stacks-annas-toolbar__pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 8px 12px;
        border-radius: 999px;
        background: rgba(37, 99, 235, 0.18);
        border: 1px solid rgba(96, 165, 250, 0.28);
        font-size: 13px;
      }

      #stacks-annas-toast-container {
        position: fixed;
        top: 72px;
        right: 20px;
        z-index: 10000;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }

      .stacks-annas-toast {
        min-width: 280px;
        max-width: 380px;
        padding: 14px 16px;
        border-radius: 10px;
        color: #f8fafc;
        background: #0f172a;
        border-left: 4px solid #38bdf8;
        box-shadow: 0 18px 45px rgba(15, 23, 42, 0.32);
        transform: translateX(24px);
        opacity: 0;
        transition: transform 0.18s ease, opacity 0.18s ease;
      }

      .stacks-annas-toast.show {
        transform: translateX(0);
        opacity: 1;
      }

      .stacks-annas-toast.success {
        border-left-color: #22c55e;
      }

      .stacks-annas-toast.error {
        border-left-color: #ef4444;
      }

      .stacks-annas-toast.info {
        border-left-color: #38bdf8;
      }

      .stacks-annas-btn-container {
        display: inline-flex;
        gap: 6px;
        align-items: center;
        vertical-align: middle;
      }

      .stacks-annas-subfolder-select {
        padding: 4px 8px;
        border: 1px solid #2563eb;
        border-radius: 3px;
        font-size: 12px;
        background: #fff;
        color: #2563eb;
        cursor: pointer;
      }

      @media (max-width: 720px) {
        #stacks-annas-toolbar {
          align-items: flex-start;
          flex-direction: column;
        }

        #stacks-annas-toast-container {
          top: 96px;
          left: 12px;
          right: 12px;
        }

        .stacks-annas-toast {
          min-width: 0;
          max-width: none;
        }
      }
    `;
    document.head.appendChild(style);
  }

  function getToastContainer() {
    let container = document.getElementById("stacks-annas-toast-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "stacks-annas-toast-container";
      document.body.appendChild(container);
    }
    return container;
  }

  function showToast({ message, type = "info", timeout = 3200 }) {
    const toast = document.createElement("div");
    toast.className = `stacks-annas-toast ${type}`;
    toast.textContent = message;
    getToastContainer().appendChild(toast);

    requestAnimationFrame(() => {
      toast.classList.add("show");
    });

    window.setTimeout(() => {
      toast.classList.remove("show");
      window.setTimeout(() => toast.remove(), 180);
    }, timeout);
  }

  function createToolbar() {
    if (document.getElementById("stacks-annas-toolbar")) return;

    const domain = document.body.dataset.stacksProxyDomain || "anna";
    const originalUrl = document.body.dataset.stacksProxyUrl || `https://${domain}/`;
    const toolbar = document.createElement("div");
    toolbar.id = "stacks-annas-toolbar";
    toolbar.innerHTML = `
      <div class="stacks-annas-toolbar__brand">
        <a href="/">Stacks</a>
        <span class="stacks-annas-toolbar__pill">Proxying ${domain}</span>
      </div>
      <div class="stacks-annas-toolbar__links">
        <a class="stacks-annas-toolbar__pill" href="/">Dashboard</a>
        <a class="stacks-annas-toolbar__pill" href="/aa/" target="_self">Anna Proxy Home</a>
      </div>
    `;

    const originalLink = document.createElement("a");
    originalLink.className = "stacks-annas-toolbar__pill";
    originalLink.href = originalUrl;
    originalLink.target = "_blank";
    originalLink.rel = "noopener noreferrer";
    originalLink.textContent = "Open Original";
    toolbar.querySelector(".stacks-annas-toolbar__links").appendChild(originalLink);

    document.body.prepend(toolbar);
  }

  async function fetchSubdirectories() {
    try {
      const response = await fetch("/api/subdirs", { credentials: "same-origin" });
      if (!response.ok) return;
      const data = await response.json();
      subdirectories = Array.isArray(data.subdirectories) ? data.subdirectories : [];
    } catch (error) {
      console.error("Failed to load subdirectories:", error);
    }
  }

  function extractMD5(url) {
    const match = MD5_REGEX.exec(url);
    return match ? match[1].toLowerCase() : null;
  }

  async function addToQueue(md5, subfolder) {
    const response = await fetch("/api/queue/add", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        md5,
        source: "annas-proxy",
        subfolder: subfolder || null,
      }),
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || data.message || `Request failed (${response.status})`);
    }
    return data;
  }

  function findSaveButton(root = document) {
    return Array.from(root.querySelectorAll('a[href="#"]')).find((anchor) => {
      return anchor.innerHTML.includes("bookmark") && anchor.textContent.includes("Save");
    });
  }

  function createSubfolderSelect() {
    if (!subdirectories.length) return null;

    const select = document.createElement("select");
    select.className = "stacks-annas-subfolder-select";
    select.title = "Select destination folder";

    const defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = "[base folder]";
    select.appendChild(defaultOption);

    subdirectories.forEach((subdir) => {
      const option = document.createElement("option");
      option.value = subdir;
      option.textContent = subdir.replace(/^\/+/, "");
      select.appendChild(option);
    });

    return select;
  }

  function createDownloadButton(md5) {
    const container = document.createElement("div");
    container.className = "stacks-annas-btn-container";

    const button = document.createElement("a");
    button.href = "#";
    button.className =
      "custom-a text-[#2563eb] inline-block outline-offset-[-2px] outline-2 rounded-[3px] focus:outline font-semibold text-sm leading-none hover:opacity-80 relative";
    button.innerHTML =
      '<span class="text-[15px] align-text-bottom inline-block icon-[typcn--download] mr-[1px]"></span>Download';

    const subfolderSelect = createSubfolderSelect();
    container.appendChild(button);
    if (subfolderSelect) {
      container.appendChild(subfolderSelect);
    }

    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();

      const originalMarkup = button.innerHTML;
      button.innerHTML =
        '<span class="text-[15px] align-text-bottom inline-block icon-[svg-spinners--ring-resize] mr-[1px]"></span>Adding...';
      button.style.pointerEvents = "none";

      try {
        const result = await addToQueue(md5, subfolderSelect ? subfolderSelect.value : null);
        if (result.success) {
          button.innerHTML =
            '<span class="text-[15px] align-text-bottom inline-block icon-[mdi--check] mr-[1px]"></span>Queued';
          showToast({ message: "Added to Stacks queue", type: "success" });
        } else {
          button.innerHTML = originalMarkup;
          showToast({ message: result.message || "Already queued", type: "info" });
        }
      } catch (error) {
        button.innerHTML = originalMarkup;
        showToast({ message: error.message, type: "error", timeout: 5000 });
      } finally {
        window.setTimeout(() => {
          button.innerHTML = originalMarkup;
          button.style.pointerEvents = "auto";
        }, 1800);
      }
    });

    return container;
  }

  function injectIntoSearchResults() {
    const items = document.querySelectorAll(".flex.pt-3.pb-3.border-b");
    items.forEach((item) => {
      const mainLink = item.querySelector("a.js-vim-focus.custom-a");
      const saveButton = findSaveButton(item);
      if (!mainLink || !saveButton || saveButton.dataset.stacksHasDownload === "1") return;

      const md5 = extractMD5(mainLink.href);
      if (!md5) return;

      saveButton.dataset.stacksHasDownload = "1";
      const separator = document.createTextNode(" · ");
      const downloadButton = createDownloadButton(md5);
      saveButton.parentNode.insertBefore(separator, saveButton.nextSibling);
      saveButton.parentNode.insertBefore(downloadButton, separator.nextSibling);
    });
  }

  function injectIntoDetailPage() {
    const md5 = extractMD5(window.location.href);
    const saveButton = findSaveButton(document);
    if (!md5 || !saveButton || saveButton.dataset.stacksHasDownload === "1") return;

    saveButton.dataset.stacksHasDownload = "1";
    const separator = document.createTextNode(" · ");
    const downloadButton = createDownloadButton(md5);
    saveButton.parentNode.insertBefore(separator, saveButton.nextSibling);
    saveButton.parentNode.insertBefore(downloadButton, separator.nextSibling);
  }

  function initObservers(pathname) {
    if (!pathname.startsWith("/aa/search")) return;

    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        injectIntoSearchResults();
      });
    });

    observer.observe(document.body, { childList: true, subtree: true });
  }

  async function init() {
    injectStyles();
    createToolbar();
    await fetchSubdirectories();

    if (window.location.pathname.startsWith("/aa/search")) {
      injectIntoSearchResults();
    } else if (window.location.pathname.startsWith("/aa/md5/")) {
      injectIntoDetailPage();
    }

    initObservers(window.location.pathname);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
