/**
 * Recoverable drafts for the entry add forms.
 *
 * While an enabled form is filled in, its widget values are debounce-saved to
 * a per-(account, form, child) server slot. Reopening the page (also on another
 * device logged into the same account) restores the slot; a successful submit
 * lets the server delete it. Everything here only ever writes form control
 * values - draft content is never inserted as HTML.
 */
(function () {
  "use strict";

  var form = document.querySelector("form[data-draft-key]");
  if (!form) {
    return;
  }

  var key = form.getAttribute("data-draft-key");
  var endpoint = form.getAttribute("data-draft-url");
  var bound = form.getAttribute("data-draft-bound") === "true";
  var csrfInput = form.querySelector('input[name="csrfmiddlewaretoken"]');
  var csrf = csrfInput ? csrfInput.value : "";

  var i18n = {
    restored: "Recovered your unsaved entry from",
    just_now: "just now",
    minutes_ago: "minutes ago",
    hours_ago: "hours ago",
    days_ago: "days ago",
    switch_confirm: "This child has an unsaved draft. Load it?",
  };
  var i18nNode = form.querySelector("[data-draft-i18n]");
  if (i18nNode) {
    try {
      var translated = JSON.parse(i18nNode.textContent);
      Object.keys(translated).forEach(function (name) {
        if (translated[name]) {
          i18n[name] = translated[name];
        }
      });
    } catch (error) {
      /* The English fallback above stays in place. */
    }
  }

  var banner = form.querySelector("[data-draft-banner]");
  var bannerMessage = form.querySelector("[data-draft-message]");
  var discardButton = form.querySelector("[data-draft-discard]");

  var state = {
    child: form.getAttribute("data-draft-child") || currentChild(),
    dirty: false,
    switching: false,
    submitted: false,
  };
  var saveTimer = null;
  var pristine = serialize();

  // -- Transport -----------------------------------------------------------

  function slotUrl(childId) {
    return (
      endpoint +
      "?form_key=" +
      encodeURIComponent(key) +
      "&child=" +
      encodeURIComponent(childId || "")
    );
  }

  function request(method, url, body) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open(method, url);
      xhr.setRequestHeader("Accept", "application/json");
      // CSRF is required for every unsafe method, with or without a body.
      if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
        xhr.setRequestHeader("X-CSRFTOKEN", csrf);
      }
      if (body !== undefined) {
        xhr.setRequestHeader("Content-Type", "application/json");
      }
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(xhr.responseText ? JSON.parse(xhr.responseText) : {});
        } else if (xhr.status === 404) {
          // No draft in this slot is a normal answer.
          resolve(null);
        } else {
          reject(new Error(xhr.status));
        }
      };
      xhr.onerror = function () {
        reject(new Error("network"));
      };
      xhr.send(body !== undefined ? JSON.stringify(body) : null);
    });
  }

  // -- Form (de)serialization ----------------------------------------------

  function currentChild() {
    var checked = form.querySelector(
      'input[type="radio"][name="child"]:checked',
    );
    return checked ? checked.value : "";
  }

  function setChild(childId) {
    var radios = form.querySelectorAll('input[type="radio"][name="child"]');
    radios.forEach(function (radio) {
      radio.checked = String(radio.value) === String(childId);
    });
  }

  function serialize() {
    var data = {};
    var controls = form.querySelectorAll("[name]");
    Array.prototype.forEach.call(controls, function (element) {
      var name = element.name;
      if (
        !name ||
        element.disabled ||
        name === "csrfmiddlewaretoken" ||
        // Django renders initial-<field> hidden inputs for dynamic defaults.
        name.indexOf("initial-") === 0 ||
        element.type === "file"
      ) {
        return;
      }
      if (element.type === "radio") {
        if (Object.prototype.hasOwnProperty.call(data, name)) {
          return;
        }
        var checked = form.querySelector(
          'input[type="radio"][name="' + name + '"]:checked',
        );
        data[name] = checked ? checked.value : "";
      } else if (element.type === "checkbox") {
        data[name] = element.checked;
      } else {
        data[name] = element.value;
      }
    });
    return data;
  }

  function applyData(data) {
    Object.keys(data).forEach(function (name) {
      var elements = form.querySelectorAll('[name="' + name + '"]');
      if (!elements.length) {
        return;
      }
      if (elements[0].type === "radio") {
        Array.prototype.forEach.call(elements, function (element) {
          element.checked = element.value === String(data[name]);
        });
      } else if (elements[0].type === "checkbox") {
        elements[0].checked = Boolean(data[name]);
      } else {
        elements[0].value =
          data[name] === null || data[name] === undefined ? "" : data[name];
      }
    });
    restoreTags(data.tags);
    openAdvancedFields();
  }

  function parseTagNames(value) {
    var names = [];
    var pattern = /"((?:[^"\\]|\\.)*)"/g;
    var match;
    while ((match = pattern.exec(value || ""))) {
      names.push(match[1]);
    }
    return names;
  }

  function restoreTags(value) {
    var tagsInput = form.querySelector('input[type="hidden"][name="tags"]');
    if (!tagsInput) {
      return;
    }
    tagsInput.value = value || "";
    tagsInput.dispatchEvent(
      new CustomEvent("babybuddy:draft-tags-restore", {
        bubbles: true,
        detail: { names: parseTagNames(value) },
      }),
    );
  }

  function openAdvancedFields() {
    Array.prototype.forEach.call(
      form.querySelectorAll("details.advanced-fields"),
      function (details) {
        var hasValue = false;
        Array.prototype.forEach.call(
          details.querySelectorAll("[name]"),
          function (element) {
            if (
              (element.type === "checkbox" && element.checked) ||
              (element.type !== "checkbox" && element.value)
            ) {
              hasValue = true;
            }
          },
        );
        if (hasValue) {
          details.open = true;
        }
      },
    );
  }

  function isContentful(data) {
    return Object.keys(data).some(function (name) {
      if (name === "child") {
        return false;
      }
      return data[name] !== "" && data[name] !== false && data[name] != null;
    });
  }

  function withoutChild(data) {
    var copy = {};
    Object.keys(data).forEach(function (name) {
      if (name !== "child") {
        copy[name] = data[name];
      }
    });
    return copy;
  }

  function matchesPristine(data) {
    // The child pick is compared separately, so selecting a child alone does
    // not count as editing the default form.
    return (
      JSON.stringify(withoutChild(data)) ===
      JSON.stringify(withoutChild(pristine))
    );
  }

  // -- Saving --------------------------------------------------------------

  function buildPayload(targetChild, data) {
    // The stored child must always be the slot being written - never the
    // newly checked radio while switching away.
    data.child = targetChild ? String(targetChild) : "";
    return {
      form_key: key,
      child: targetChild ? parseInt(targetChild, 10) : null,
      data: data,
    };
  }

  function saveNow(childOverride) {
    if (state.submitted) {
      return Promise.resolve();
    }
    var data = serialize();
    var target = childOverride !== undefined ? childOverride : state.child;
    // Default times already make an untouched form "contentful", so only
    // values that actually differ from the rendered defaults start a draft.
    if (matchesPristine(data) || !isContentful(data)) {
      return Promise.resolve();
    }
    return request("POST", endpoint, buildPayload(target, data));
  }

  function scheduleSave() {
    if (state.submitted || state.switching) {
      return;
    }
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      var data = serialize();
      if (matchesPristine(data)) {
        return;
      }
      state.dirty = true;
      saveNow();
    }, 800);
  }

  function flushSave() {
    if (state.submitted || !state.dirty) {
      return;
    }
    var data = serialize();
    if (matchesPristine(data) || !isContentful(data)) {
      return;
    }
    var payload = JSON.stringify(buildPayload(state.child, data));
    // Best effort: the page may be getting suspended or closed.
    if (window.fetch) {
      try {
        fetch(endpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFTOKEN": csrf,
          },
          body: payload,
          credentials: "same-origin",
          keepalive: true,
        });
      } catch (error) {
        /* The debounced save already covers the normal case. */
      }
    }
    state.dirty = false;
  }

  // -- Restoring -----------------------------------------------------------

  function relativeTime(iso) {
    var elapsedMs = Date.now() - new Date(iso).getTime();
    var minutes = Math.round(elapsedMs / 60000);
    if (minutes < 1) {
      return i18n.just_now;
    }
    if (minutes < 60) {
      return minutes + " " + i18n.minutes_ago;
    }
    var hours = Math.round(minutes / 60);
    if (hours < 24) {
      return hours + " " + i18n.hours_ago;
    }
    return Math.round(hours / 24) + " " + i18n.days_ago;
  }

  function showBanner(updated) {
    if (!banner || !bannerMessage) {
      return;
    }
    bannerMessage.textContent =
      i18n.restored + " " + relativeTime(updated) + ".";
    banner.classList.remove("d-none");
  }

  function hideBanner() {
    if (banner) {
      banner.classList.add("d-none");
    }
  }

  function loadDraft(childId) {
    return request("GET", slotUrl(childId)).then(function (draft) {
      if (!draft || !draft.data) {
        return;
      }
      applyData(draft.data);
      // The slot, not possibly-stale data inside, owns the child selection.
      setChild(childId);
      state.child = String(childId || currentChild());
      showBanner(draft.updated);
    });
  }

  function confirmLoad(draft) {
    if (!window.confirm(i18n.switch_confirm)) {
      return false;
    }
    applyData(draft.data);
    setChild(state.pendingChild);
    showBanner(draft.updated);
    return true;
  }

  function onChildChange(newId) {
    if (state.switching || newId === state.child) {
      return;
    }
    var oldId = state.child;
    var ownData = serialize();
    var hasOwnChanges = !matchesPristine(ownData) && isContentful(ownData);
    state.switching = true;
    state.pendingChild = newId;
    clearTimeout(saveTimer);
    state.dirty = false;

    function settle() {
      state.child = newId;
      state.switching = false;
    }

    if (oldId === "") {
      // Nothing was chosen before. Existing content belongs to the NULL slot
      // and has to migrate into the newly chosen slot, never be discarded.
      request("GET", slotUrl(newId))
        .then(function (draft) {
          if (draft && draft.data) {
            if (!confirmLoad(draft)) {
              setChild("");
              state.switching = false;
              return;
            }
            // The NULL-slot content is superseded by the existing draft.
            return request("DELETE", slotUrl(""));
          }
          if (hasOwnChanges) {
            // Keep what was typed and move it to the chosen child.
            setChild(newId);
            return saveNow(newId).then(function () {
              return request("DELETE", slotUrl(""));
            });
          }
          applyData(pristine);
          setChild(newId);
          hideBanner();
        })
        .then(function () {
          settle();
        })
        .catch(function () {
          settle();
        });
      return;
    }

    // Switching between concrete children: save the old slot first.
    saveNow(oldId)
      .then(function () {
        return request("GET", slotUrl(newId));
      })
      .then(function (draft) {
        if (draft && draft.data) {
          if (!confirmLoad(draft)) {
            setChild(oldId);
            state.switching = false;
            return;
          }
        } else {
          applyData(pristine);
          setChild(newId);
          hideBanner();
        }
        settle();
      })
      .catch(function () {
        // Never trap the child selection because drafting had trouble.
        settle();
      });
  }

  // -- Events --------------------------------------------------------------

  ["input", "change", "click"].forEach(function (eventName) {
    form.addEventListener(eventName, function (event) {
      if (
        event.target.closest &&
        event.target.closest("[data-draft-discard]")
      ) {
        return;
      }
      if (
        eventName === "change" &&
        event.target.name === "child" &&
        event.target.checked
      ) {
        onChildChange(event.target.value);
        return;
      }
      state.dirty = true;
      scheduleSave();
    });
  });

  if (discardButton) {
    discardButton.addEventListener("click", function () {
      request("DELETE", slotUrl(state.child)).then(function () {
        applyData(pristine);
        setChild(state.child);
        state.dirty = false;
        hideBanner();
      });
    });
  }

  form.addEventListener("submit", function () {
    state.submitted = true;
    clearTimeout(saveTimer);
  });
  window.addEventListener("pagehide", flushSave);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      flushSave();
    }
  });

  // With no child preselected (e.g. a refreshed multi-child form), look for a
  // single in-progress draft across the children and resume it. Ambiguity is
  // left to the explicit switch-confirmation flow.
  function autodiscoverDraft() {
    var childIds = [""];
    Array.prototype.forEach.call(
      form.querySelectorAll('input[type="radio"][name="child"]'),
      function (radio) {
        if (radio.value && childIds.indexOf(radio.value) === -1) {
          childIds.push(radio.value);
        }
      },
    );
    return Promise.all(
      childIds.map(function (childId) {
        return request("GET", slotUrl(childId))
          .then(function (draft) {
            return draft ? { id: childId, draft: draft } : null;
          })
          .catch(function () {
            return null;
          });
      }),
    ).then(function (results) {
      var found = results.filter(Boolean);
      if (found.length !== 1) {
        return;
      }
      var id = found[0].id;
      if (id) {
        setChild(id);
        state.child = id;
      }
      applyData(found[0].draft.data);
      showBanner(found[0].draft.updated);
    });
  }

  // A rejected submit is re-rendered already populated; do not overwrite it
  // with an older draft, just keep editing from the rendered values.
  if (!bound) {
    if (state.child) {
      loadDraft(state.child);
    } else {
      autodiscoverDraft();
    }
  }
})();
