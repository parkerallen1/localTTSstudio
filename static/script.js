/*
 * Local TTS Studio — frontend logic.
 *
 * The single client-side script behind index.html. It talks to the FastAPI
 * backend (main.py) over the JSON API and drives the whole UI. Everything runs
 * inside one DOMContentLoaded handler; module-scoped state (paragraphsData,
 * currentProjectId, etc.) lives at the top of that closure.
 *
 * Main responsibilities:
 *   • Parsing — turn pasted Markdown into paragraph cards (see the "Parse text
 *     area into paragraphs" handler). Strips Markdown markers (stripMarkdown),
 *     drops the Time/Focus/Scriptures metadata block, names the project from the
 *     first line, marks each `##` heading as a chapter start (except the
 *     boilerplate headings in CHAPTER_EXCLUDE), and merges short lines together
 *     (combineShortParagraphs). Bible-specific cleanup is opt-in (cleanTextBible).
 *   • Generation — synthesize audio per paragraph or for all of them, with a
 *     stop control and live status badges.
 *   • Projects — auto-create/save/load projects via /api/projects; audio is
 *     fetched from the server. The Open-project modal lists/loads/deletes them.
 *   • Voice profiles & settings — manage CustomVoice profiles and the settings
 *     modal (model size/type, model cache, diagnostics).
 *   • Export — download merged audio (WAV/M4A) and copy a chapter shortcode.
 *   • Activity log (SSE) and the in-app update flow.
 *
 * NOTE: this file is served as a static asset, so browsers cache it — hard-
 * refresh after changes during development.
 */
document.addEventListener('DOMContentLoaded', () => {
    const textInput = document.getElementById('text-input');
    const btnParse = document.getElementById('btn-parse');
    const paragraphsContainer = document.getElementById('paragraphs-container');
    const paragraphsList = document.getElementById('paragraphs-list');
    const paraCountSpan = document.getElementById('para-count');
    const btnGenerateAll = document.getElementById('btn-generate-all');
    const btnStopGeneration = document.getElementById('btn-stop-generation');
    const btnDownloadAll = document.getElementById('btn-download-all');
    const btnCopyChapters = document.getElementById('btn-copy-chapters');
    const modelTypeSelect = document.getElementById('model-type-select');
    const speakerSelect = document.getElementById('speaker-select');
    const voiceDesignPrompt = document.getElementById('voice-design-prompt');
    const modelSizeSelect = document.getElementById('model-size-select');

    // Auto-Updater
    const updateBanner = document.getElementById('update-banner');
    const updateVersionSpan = document.getElementById('update-version');
    const btnDoUpdate = document.getElementById('btn-do-update');
    let otaDownloadUrl = null;

    const downloadOptions = document.getElementById('download-options');

    // --- Format Toggle ---
    let selectedFormat = 'wav';
    document.querySelectorAll('.format-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.format-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            selectedFormat = btn.dataset.format;
        });
    });

    // --- Project Elements ---
    const projectNameInput = document.getElementById('project-name-input');
    const btnOpenProject = document.getElementById('btn-open-project');
    const saveStatusEl = document.getElementById('save-status');
    const projectsModal = document.getElementById('projects-modal');
    const btnCloseProjectsModal = document.getElementById('btn-close-projects-modal');
    const projectsListEl = document.getElementById('projects-list');
    const noProjectsMsg = document.getElementById('no-projects-msg');

    // ─── Activity Log Drawer ───────────────────────────────────────────────────
    const logEntries = document.getElementById('activity-log-entries');
    const btnMinimizeLog = document.getElementById('btn-minimize-log');
    const btnCopyLog = document.getElementById('btn-copy-log');
    const btnClearLog = document.getElementById('btn-clear-log');
    const btnToggleLog = document.getElementById('btn-toggle-log');
    const activityLogEl = document.getElementById('activity-log');
    let logMinimized = false;
    // Stores plain-text versions of all entries for clipboard copying
    const logPlainLines = [];

    function log(msg, level = 'info', source = 'client') {
        const entry = document.createElement('div');
        entry.className = `log-entry ${level}`;
        const now = new Date();
        const time = now.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const timeSpan = document.createElement('span');
        timeSpan.className = 'log-time';
        timeSpan.textContent = time;
        const srcBadge = document.createElement('span');
        srcBadge.className = `log-source log-source-${source}`;
        srcBadge.textContent = source === 'server' ? 'SRV' : 'UI';
        const msgSpan = document.createElement('span');
        msgSpan.className = 'log-msg';
        msgSpan.textContent = msg;
        entry.appendChild(timeSpan);
        entry.appendChild(srcBadge);
        entry.appendChild(msgSpan);
        logEntries.appendChild(entry);

        // Store plain-text line for "Copy all"
        logPlainLines.push(`${time} [${level.toUpperCase()}] ${msg}`);

        // Auto-scroll unless user has scrolled up
        const atBottom = logEntries.scrollHeight - logEntries.scrollTop - logEntries.clientHeight < 40;
        if (atBottom) logEntries.scrollTop = logEntries.scrollHeight;
    }

    btnMinimizeLog.addEventListener('click', () => {
        logMinimized = !logMinimized;
        logEntries.style.display = logMinimized ? 'none' : '';
        btnMinimizeLog.textContent = logMinimized ? '+' : '−';
        activityLogEl.style.height = logMinimized ? 'auto' : '';
    });

    // "Logs" toggle button in header
    btnToggleLog.addEventListener('click', () => {
        const hidden = activityLogEl.style.display === 'none';
        activityLogEl.style.display = hidden ? '' : 'none';
        btnToggleLog.textContent = hidden ? 'Hide Logs' : 'Logs';
    });

    btnCopyLog.addEventListener('click', () => {
        const text = logPlainLines.join('\n');
        navigator.clipboard.writeText(text).then(() => {
            const orig = btnCopyLog.textContent;
            btnCopyLog.textContent = 'Copied!';
            setTimeout(() => { btnCopyLog.textContent = orig; }, 1500);
        }).catch(() => {
            // Fallback for older browsers
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        });
    });

    btnClearLog.addEventListener('click', () => {
        logEntries.innerHTML = '';
        logPlainLines.length = 0;
    });

    // --- Server Activity Log SSE ---
    // The handler pushes entries directly into the Activity Log drawer above.
    (function connectActivityLog() {
        const src = new EventSource('/api/activity_log');
        src.onmessage = (event) => {
            try {
                const entries = JSON.parse(event.data);
                entries.forEach(e => log(e.msg, e.level || 'info', 'server'));
            } catch (_) { /* ignored */ }
        };
        src.onerror = () => {
            // Reconnect silently — EventSource handles reconnections automatically
        };
    })();

    log('Studio ready.', 'ok');

    const configCustomVoice = document.getElementById('config-custom-voice');
    const configCustomVoiceInstruct = document.getElementById('config-custom-voice-instruct');
    const configVoiceDesign = document.getElementById('config-voice-design');
    const configBase = document.getElementById('config-base');
    const customVoiceInstruct = document.getElementById('custom-voice-instruct');
    const temperatureSlider = document.getElementById('temperature-slider');
    const temperatureValue = document.getElementById('temperature-value');
    temperatureSlider.addEventListener('input', () => {
        temperatureValue.textContent = parseFloat(temperatureSlider.value).toFixed(2);
    });

    // --- Auto-Updater Logic ---
    async function checkForUpdates() {
        try {
            const res = await fetch('/api/check_update');
            const data = await res.json();
            if (data.update_available && data.download_url) {
                otaDownloadUrl = data.download_url;
                updateVersionSpan.textContent = data.latest_version;
                updateBanner.classList.remove('hidden');
            }
        } catch (e) {
            console.error("Failed to check for updates:", e);
        }
    }

    btnDoUpdate.addEventListener('click', async () => {
        if (!otaDownloadUrl) return;

        btnDoUpdate.disabled = true;
        btnDoUpdate.textContent = "Updating... Please wait";
        log("Downloading and installing update. The app will restart shortly...");

        try {
            const formData = new FormData();
            formData.append("download_url", otaDownloadUrl);
            const res = await fetch('/api/do_update', {
                method: 'POST',
                body: formData
            });
            const data = await res.json();
            console.log("Update response:", data);
        } catch (e) {
            console.warn("Update fetch threw an error (likely server disconnected). Proceeding with reload assumption.", e);
        }

        // The python server shuts itself down to replace the files, so we ping until it's back up, then reload
        setInterval(async () => {
            try {
                await fetch('/', { mode: 'no-cors' });
                window.location.reload();
            } catch (err) { }
        }, 2000);
    });

    // Run update check in background immediately on load
    checkForUpdates();

    // Profile Elements
    const savedVoiceSelect = document.getElementById('saved-voice-select');
    const btnOpenSaveModal = document.getElementById('btn-open-save-modal');
    const saveProfileModal = document.getElementById('save-profile-modal');
    const btnCloseModal = document.getElementById('btn-close-modal');
    const btnSaveProfile = document.getElementById('btn-save-profile');
    const btnDeleteProfile = document.getElementById('btn-delete-profile');
    const newProfileName = document.getElementById('new-profile-name');
    const newProfileAudio = document.getElementById('new-profile-audio');
    const newProfileText = document.getElementById('new-profile-text');

    let paragraphsData = [];
    let insertSeq = 0; // monotonic counter for unique ids on inserted paragraphs
    let currentProjectId = null;
    let hasUnsavedChanges = false;
    let autoSaveTimer = null;
    let generationStopped = false;
    let activeAbortController = null;
    let activeGenerationCount = 0;

    // ─── Project Management ───────────────────────────────────────────────────

    function setSaveStatus(state) {
        if (!saveStatusEl) return;
        if (state === 'saving') {
            saveStatusEl.textContent = 'Saving...';
            saveStatusEl.classList.add('saving');
        } else {
            saveStatusEl.textContent = 'Saved';
            saveStatusEl.classList.remove('saving');
        }
    }

    function scheduleAutoSave() {
        hasUnsavedChanges = true;
        clearTimeout(autoSaveTimer);
        autoSaveTimer = setTimeout(async () => {
            if (!currentProjectId) return;
            setSaveStatus('saving');
            try {
                await saveCurrentProject();
                setSaveStatus('saved');
            } catch (e) {
                console.warn('Auto-save failed:', e);
            }
        }, 1500);
    }

    function getCurrentSettings() {
        return {
            modelType: modelTypeSelect.value,
            modelSize: modelSizeSelect ? modelSizeSelect.value : '1.7B',
            speaker: speakerSelect.value,
            voiceDesignPrompt: voiceDesignPrompt.value,
            savedVoiceId: savedVoiceSelect.value,
            bibleMode: document.getElementById('bible-text-mode') ? document.getElementById('bible-text-mode').checked : false
        };
    }

    function markUnsaved() { scheduleAutoSave(); }

    window.addEventListener('beforeunload', (e) => {
        if (hasUnsavedChanges) {
            e.preventDefault();
            e.returnValue = '';
        }
    });

    function revokeAllBlobUrls() {
        paragraphsData.forEach(p => {
            if (p.audioUrl && p.audioUrl.startsWith('blob:')) {
                URL.revokeObjectURL(p.audioUrl);
            }
        });
    }

    async function ensureProject() {
        if (currentProjectId) return;
        const name = projectNameInput.value.trim() || 'Untitled Project';
        try {
            const res = await fetch('/api/projects', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, settings: getCurrentSettings() })
            });
            const project = await res.json();
            currentProjectId = project.id;
            projectNameInput.value = project.name;
            log(`Project "${project.name}" created.`, 'ok');
        } catch (e) {
            console.error('Failed to create project:', e);
        }
    }

    async function saveCurrentProject() {
        if (!currentProjectId) return;
        const name = projectNameInput.value.trim() || 'Untitled Project';
        const payload = {
            name,
            settings: getCurrentSettings(),
            rawText: textInput.value,
            paragraphs: paragraphsData.map(p => ({
                id: p.id,
                text: p.text,
                hasAudio: p.status === 'done',
                isChapter: p.chapter
            }))
        };
        await fetch(`/api/projects/${currentProjectId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        hasUnsavedChanges = false;
        setSaveStatus('saved');
    }

    async function autoSaveParagraphAudio(index, blob) {
        if (!currentProjectId) return;
        const para = paragraphsData[index];
        const formData = new FormData();
        formData.append('audio', blob, 'audio.wav');
        const res = await fetch(`/api/projects/${currentProjectId}/audio/${para.id}`, {
            method: 'POST',
            body: formData
        });
        if (!res.ok) {
            // Audio file didn't land on disk — mark paragraph as failed so it
            // doesn't get saved as hasAudio:true and show as "Ready" on reload
            para.status = 'error';
            para.audioBlob = null;
            if (para.audioUrl && para.audioUrl.startsWith('blob:')) URL.revokeObjectURL(para.audioUrl);
            para.audioUrl = null;
            updateCardUi(index);
            updateDownloadButtonVisibility();
            log(`Para ${index + 1} audio save failed — will need to regenerate.`, 'error');
            return;
        }
        await saveCurrentProject();
    }

    async function loadProject(projectId) {
        try {
            const res = await fetch(`/api/projects/${projectId}`);
            if (!res.ok) throw new Error('Failed to load project');
            const project = await res.json();

            revokeAllBlobUrls();

            currentProjectId = projectId;
            projectNameInput.value = project.name;

            // Restore settings
            const s = project.settings || {};
            if (s.modelType) modelTypeSelect.value = s.modelType;
            if (s.modelSize && modelSizeSelect) modelSizeSelect.value = s.modelSize;
            if (s.speaker) speakerSelect.value = s.speaker;
            if (s.voiceDesignPrompt !== undefined) voiceDesignPrompt.value = s.voiceDesignPrompt;
const bibleCheckbox = document.getElementById('bible-text-mode');
            if (bibleCheckbox && s.bibleMode !== undefined) bibleCheckbox.checked = s.bibleMode;
            applyModelTypeConfig();
            if (s.savedVoiceId) {
                await loadProfiles();
                savedVoiceSelect.value = s.savedVoiceId;
                savedVoiceSelect.dispatchEvent(new Event('change'));
            }

            // Restore paragraphs — audio served from server
            paragraphsData = (project.paragraphs || []).map(p => ({
                id: p.id,
                text: p.text,
                status: p.hasAudio ? 'done' : 'idle',
                audioBlob: null,
                audioUrl: p.hasAudio ? `/api/projects/${projectId}/audio/${p.id}` : null,
                chapter: !!p.isChapter
            }));

            // Restore raw textarea text (fall back to joining paragraphs for older projects)
            textInput.value = project.rawText || paragraphsData.map(p => p.text).join('\n');

            if (paragraphsData.length > 0) {
                renderParagraphs();
                paragraphsContainer.classList.remove('hidden');
                paraCountSpan.textContent = paragraphsData.length;
            } else {
                paragraphsContainer.classList.add('hidden');
            }
            updateDownloadButtonVisibility();
            projectsModal.classList.add('hidden');
            hasUnsavedChanges = false;
            setSaveStatus('saved');
            log(`Loaded project: "${project.name}"`, 'ok');
        } catch (e) {
            log(`Failed to load project: ${e.message}`, 'error');
        }
    }

    window.loadProjectFromModal = (id) => loadProject(id);

    window.deleteProjectFromModal = async (id, name) => {
        if (!confirm(`Delete project "${name}"? This cannot be undone.`)) return;
        try {
            await fetch(`/api/projects/${id}`, { method: 'DELETE' });
            if (currentProjectId === id) {
                currentProjectId = null;
                projectNameInput.value = '';
                paragraphsData = [];
                textInput.value = '';
                paragraphsContainer.classList.add('hidden');
                updateDownloadButtonVisibility();
                setSaveStatus('saved');
            }
            log(`Deleted project: "${name}"`);
            openProjectsModal(); // refresh list
        } catch (e) {
            log(`Failed to delete project: ${e.message}`, 'error');
        }
    };

    async function openProjectsModal() {
        projectsModal.classList.remove('hidden');
        try {
            const res = await fetch('/api/projects');
            const projects = await res.json();
            projectsListEl.innerHTML = '';
            if (projects.length === 0) {
                noProjectsMsg.classList.remove('hidden');
            } else {
                noProjectsMsg.classList.add('hidden');
                projects.forEach(p => {
                    const item = document.createElement('div');
                    item.className = 'project-item' + (p.id === currentProjectId ? ' project-item-active' : '');
                    item.innerHTML = `
                        <div class="project-item-info">
                            <div class="project-item-name">${escapeHtml(p.name)}</div>
                        </div>
                        <div class="project-item-actions">
                            <button class="secondary-btn small-btn" onclick="loadProjectFromModal('${p.id}')">Open</button>
                            <button class="secondary-btn small-btn danger-btn" onclick="deleteProjectFromModal('${p.id}', '${escapeHtml(p.name).replace(/'/g, "\\'")}')">Delete</button>
                        </div>`;
                    projectsListEl.appendChild(item);
                });
            }
        } catch (e) {
            log('Failed to load projects list', 'error');
        }
    }

    async function createNewProject() {
        // Reset settings to defaults
        modelTypeSelect.value = 'Base';
        if (modelSizeSelect) modelSizeSelect.value = '1.7B';
        speakerSelect.value = 'Vivian';
        voiceDesignPrompt.value = '';
        const bibleCheckbox = document.getElementById('bible-text-mode');
        if (bibleCheckbox) bibleCheckbox.checked = false;
        applyModelTypeConfig();

        try {
            const res = await fetch('/api/projects', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: 'Untitled Project', settings: getCurrentSettings() })
            });
            const project = await res.json();
            revokeAllBlobUrls();
            currentProjectId = project.id;
            projectNameInput.value = project.name;
            projectNameInput.select(); // select name so user can type immediately
            paragraphsData = [];
            textInput.value = '';
            paragraphsContainer.classList.add('hidden');
            updateDownloadButtonVisibility();
            hasUnsavedChanges = false;
            setSaveStatus('saved');
            projectsModal.classList.add('hidden');
            log(`New project created.`, 'ok');
        } catch (e) {
            log(`Failed to create project: ${e.message}`, 'error');
        }
    }

    // Wire up "New Project" button inside the Open modal
    document.getElementById('btn-new-project').addEventListener('click', createNewProject);

    btnOpenProject.addEventListener('click', () => openProjectsModal());
    btnCloseProjectsModal.addEventListener('click', () => projectsModal.classList.add('hidden'));

    // Project name auto-saves on blur (like Google Docs)
    projectNameInput.addEventListener('blur', async () => {
        if (!currentProjectId) return;
        const trimmed = projectNameInput.value.trim();
        if (!trimmed) projectNameInput.value = 'Untitled Project';
        setSaveStatus('saving');
        try {
            await saveCurrentProject();
        } catch (e) {
            console.warn('Name save failed:', e);
        }
    });

    // Text input triggers auto-save after 1.5s of inactivity
    textInput.addEventListener('input', () => {
        if (currentProjectId) scheduleAutoSave();
    });

    // Settings changes trigger auto-save
    modelTypeSelect.addEventListener('change', () => { if (currentProjectId) scheduleAutoSave(); });
    if (modelSizeSelect) modelSizeSelect.addEventListener('change', () => { if (currentProjectId) scheduleAutoSave(); });
    speakerSelect.addEventListener('change', () => { if (currentProjectId) scheduleAutoSave(); });
    voiceDesignPrompt.addEventListener('input', () => { if (currentProjectId) scheduleAutoSave(); });

    // ─── Keyboard Shortcuts ───────────────────────────────────────────────────
    document.addEventListener('keydown', (e) => {
        // Escape — close any open modal
        if (e.key === 'Escape') {
            const saveProfileModal = document.getElementById('save-profile-modal');
            const settingsModal = document.getElementById('settings-modal');
            if (settingsModal && !settingsModal.classList.contains('hidden')) {
                settingsModal.classList.add('hidden');
            } else if (projectsModal && !projectsModal.classList.contains('hidden')) {
                projectsModal.classList.add('hidden');
            } else if (saveProfileModal && !saveProfileModal.classList.contains('hidden')) {
                saveProfileModal.classList.add('hidden');
            }
        }
        // Cmd/Ctrl+Enter — generate all
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            if (btnGenerateAll && !btnGenerateAll.disabled) btnGenerateAll.click();
        }
    });

    // ─────────────────────────────────────────────────────────────────────────

    function setGeneratingState(active) {
        if (active) {
            btnGenerateAll.classList.add('hidden');
            btnStopGeneration.classList.remove('hidden');
        } else {
            btnGenerateAll.classList.remove('hidden');
            btnGenerateAll.disabled = false;
            btnGenerateAll.textContent = 'Generate All';
            btnStopGeneration.classList.add('hidden');
        }
    }

    btnStopGeneration.addEventListener('click', () => {
        generationStopped = true;
        if (activeAbortController) activeAbortController.abort();
        log('Generation stopped.', 'warn');
    });

    // Serialized generation pool — MPS is not thread-safe, one request at a time.
    async function runGenerationPool() {
        generationStopped = false;
        const queue = paragraphsData
            .map((p, i) => i)
            .filter(i => paragraphsData[i].status !== 'done');

        function next() {
            if (queue.length === 0 || generationStopped) return Promise.resolve();
            const i = queue.shift();
            return window.generateSingle(i).finally(() => next());
        }

        await next();
    }

    // Parse text area into paragraphs
    btnParse.addEventListener('click', async () => {
        const rawText = textInput.value.trim();
        if (!rawText) return;

        const bibleCheckbox = document.getElementById('bible-text-mode');
        const bibleMode = bibleCheckbox && bibleCheckbox.checked;

        // Parse line-by-line so Markdown H2 headings (## ) can mark chapter starts.
        // Each line becomes { text, chapter }; Markdown markers are stripped so the
        // TTS model never reads "##", "**", bullets, links, etc. aloud.
        const items = [];
        for (const rawLine of rawText.split(/\n/)) {
            const trimmed = rawLine.trim();
            if (!trimmed) continue;
            const isHeading = /^##(?!#)\s+/.test(trimmed); // H2 only — not H1 (#) or H3+ (###)
            const stripped = stripMarkdown(trimmed);
            // Drop the QQT metadata block — the labels are constant, content varies.
            if (/^(Time|Focus|Scriptures)\b[^:\n]{0,24}:/i.test(stripped)) continue;
            // Boilerplate headings start their own paragraph but are NOT chapters.
            const isChapter = isHeading && !CHAPTER_EXCLUDE.has(headingKey(stripped));
            let cleaned = cleanTextGeneral(stripped);
            if (bibleMode) cleaned = cleanTextBible(cleaned);
            cleaned = cleaned.trim();
            if (!cleaned) continue;
            items.push({ text: cleaned, chapter: isChapter, heading: isHeading });
        }

        const rawParagraphs = combineShortParagraphs(items);
        // The first paragraph is the title — always a chapter start.
        if (rawParagraphs.length) rawParagraphs[0].chapter = true;
        const batchId = Date.now();

        revokeAllBlobUrls();
        paragraphsData = rawParagraphs.map((item, index) => ({
            id: `para-${batchId}-${index}`,
            text: item.text,
            status: 'idle',
            audioBlob: null,
            audioUrl: null,
            chapter: item.chapter
        }));

        renderParagraphs();

        paragraphsContainer.classList.remove('hidden');
        paraCountSpan.textContent = paragraphsData.length;
        updateDownloadButtonVisibility();
        log(`Parsed ${paragraphsData.length} paragraph(s). Click "Generate All" to start.`);

        // Use the first non-empty line (the document's title) as the project name.
        const firstLine = rawText.split(/\n/).map(l => l.trim()).find(l => l.length > 0);
        const derivedTitle = firstLine ? stripMarkdown(firstLine).replace(/[.\s]+$/, '').trim() : '';
        if (derivedTitle) projectNameInput.value = derivedTitle;

        // Ensure a project exists for saving (created with the derived title above).
        await ensureProject();
        // If the project already existed, persist the renamed title too.
        if (derivedTitle && currentProjectId) await saveCurrentProject();
        markUnsaved();
    });

    function renderParagraphs() {
        paragraphsList.innerHTML = '';

        paragraphsData.forEach((para, index) => {
            // Insert affordance above each card
            paragraphsList.appendChild(makeInsertRow(index));

            const card = document.createElement('div');
            card.className = `paragraph-card${para.chapter ? ' is-chapter' : ''}`;
            card.id = para.id;

            card.innerHTML = `
                <div class="paragraph-card-header">
                    <div class="para-reorder-btns">
                        <button class="reorder-btn" onclick="moveParagraph(${index}, -1)" ${index === 0 ? 'disabled' : ''} title="Move up">&#8593;</button>
                        <button class="reorder-btn" onclick="moveParagraph(${index}, 1)" ${index === paragraphsData.length - 1 ? 'disabled' : ''} title="Move down">&#8595;</button>
                    </div>
                    <button class="chapter-toggle-btn ${para.chapter ? 'active' : ''}" onclick="toggleChapter(${index})" title="Mark this paragraph as a chapter start">&#9873; Chapter</button>
                    <button class="delete-para-btn" onclick="deleteParagraph(${index})" title="Remove paragraph">&times;</button>
                </div>
                <textarea class="paragraph-text-edit" oninput="handleEdit(${index}, this.value)" rows="3">${escapeHtml(para.text)}</textarea>
                <div class="card-actions">
                    <span class="status-badge ${para.status}" id="status-${para.id}">
                        ${getStatusText(para.status)}
                    </span>
                    
                    <audio id="audio-${para.id}" class="audio-player ${para.audioUrl ? '' : 'hidden'}" controls src="${para.audioUrl || ''}"></audio>
                    
                    <div class="action-buttons">
                        <button class="secondary-btn" onclick="generateSingle(${index})" id="btn-gen-${para.id}" ${para.status === 'generating' ? 'disabled' : ''}>
                            ${para.status === 'done' ? 'Regenerate' : 'Generate'}
                        </button>
                    </div>
                </div>
            `;
            paragraphsList.appendChild(card);

            // Handle broken audio (e.g. file deleted from disk after project was saved)
            const audioEl = document.getElementById(`audio-${para.id}`);
            if (audioEl && para.audioUrl) {
                audioEl.addEventListener('error', () => {
                    para.status = 'idle';
                    para.audioUrl = null;
                    para.audioBlob = null;
                    updateCardUi(index);
                    updateDownloadButtonVisibility();
                }, { once: true });
            }
        });

        // Trailing insert affordance (append at the end)
        paragraphsList.appendChild(makeInsertRow(paragraphsData.length));
    }

    function makeInsertRow(index) {
        const row = document.createElement('div');
        row.className = 'insert-row';
        row.innerHTML = `<button class="insert-para-btn" onclick="insertParagraph(${index})" title="Insert a paragraph here">+</button>`;
        return row;
    }

    function getStatusText(status) {
        switch (status) {
            case 'idle': return 'Waiting';
            case 'generating': return 'Generating...';
            case 'done': return 'Ready';
            case 'error': return 'Failed';
            case 'regenerate': return 'Regenerate';
            default: return 'Wait';
        }
    }

    // Expose handleEdit to window
    window.handleEdit = (index, newText) => {
        const para = paragraphsData[index];
        if (para.text !== newText) {
            para.text = newText;
            markUnsaved();
            if (para.status === 'done') {
                para.status = 'regenerate';
                updateCardUi(index);
                updateDownloadButtonVisibility();
            }
        }
    };

    window.deleteParagraph = (index) => {
        const para = paragraphsData[index];
        if (para.audioUrl) URL.revokeObjectURL(para.audioUrl);
        paragraphsData.splice(index, 1);
        renderParagraphs();
        paraCountSpan.textContent = paragraphsData.length;
        updateDownloadButtonVisibility();
        if (paragraphsData.length === 0) {
            paragraphsContainer.classList.add('hidden');
        }
    };

    window.moveParagraph = (index, direction) => {
        const newIndex = index + direction;
        if (newIndex < 0 || newIndex >= paragraphsData.length) return;
        [paragraphsData[index], paragraphsData[newIndex]] = [paragraphsData[newIndex], paragraphsData[index]];
        renderParagraphs();
        markUnsaved();
    };

    // Insert a new empty paragraph before the given index (length = append at end)
    window.insertParagraph = (index) => {
        paragraphsData.splice(index, 0, {
            id: `para-${Date.now()}-ins${insertSeq++}`,
            text: '',
            status: 'idle',
            audioBlob: null,
            audioUrl: null,
            chapter: false
        });
        renderParagraphs();
        paraCountSpan.textContent = paragraphsData.length;
        paragraphsContainer.classList.remove('hidden');
        updateDownloadButtonVisibility();
        markUnsaved();
    };

    window.toggleChapter = (index) => {
        const para = paragraphsData[index];
        para.chapter = !para.chapter;
        const card = document.getElementById(para.id);
        if (card) {
            card.classList.toggle('is-chapter', para.chapter);
            const btn = card.querySelector('.chapter-toggle-btn');
            if (btn) btn.classList.toggle('active', para.chapter);
        }
        updateDownloadButtonVisibility();
        markUnsaved();
    };

    // Expose to window for the inline onclick handlers
    window.generateSingle = async (index) => {
        const para = paragraphsData[index];
        if (para.status === 'generating') return;

        // Validate before committing to generation
        if (modelTypeSelect.value === 'Base' && !savedVoiceSelect.value) {
            log("Please select a saved voice profile first.", 'error');
            return;
        }

        para.status = 'generating';
        if (para.audioUrl) URL.revokeObjectURL(para.audioUrl);
        para.audioBlob = null;
        para.audioUrl = null;
        updateCardUi(index);

        const textPreview = para.text.length > 60 ? para.text.substring(0, 60) + '...' : para.text;
        const mode = modelTypeSelect.value;
        const size = modelSizeSelect ? modelSizeSelect.value : '1.7B';
        log(`[${index + 1}/${paragraphsData.length}] Generating — mode=${mode}, size=${size}`);
        log(`[${index + 1}] Text: "${textPreview}"`);
        const genStartTime = performance.now();

        // Track active generations so the stop button appears for single-para too
        activeGenerationCount++;
        if (activeGenerationCount === 1) setGeneratingState(true);
        activeAbortController = new AbortController();

        // ─── Generation fetch with 30-min timeout + download-% heartbeat ────────
        // We use a manual timeout timer (compatible with all browsers) rather than
        // AbortSignal.timeout() to merge cleanly with the existing abortController.
        let timeoutTimer = null;
        const TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes
        timeoutTimer = setTimeout(() => {
            if (activeAbortController) activeAbortController.abort('timeout');
        }, TIMEOUT_MS);

        // Heartbeat: every 2 s while generation is pending, read the shared
        // progressState (fed by the always-open /api/progress EventSource below)
        // and reflect any download % in the paragraph's status badge.
        const heartbeatInterval = setInterval(() => {
            if (progressState.status === 'downloading') {
                const badge = document.getElementById(`status-${para.id}`);
                if (badge) badge.textContent = `Downloading… ${progressState.pct}%`;
            }
        }, 2000);

        try {
            const formData = new FormData();
            formData.append("text", para.text);
            formData.append("language", "English");
            formData.append("model_size", modelSizeSelect ? modelSizeSelect.value : "1.7B");
            formData.append("model_type", modelTypeSelect.value);

            if (modelTypeSelect.value === 'CustomVoice') {
                formData.append("speaker", speakerSelect.value);
                if (customVoiceInstruct.value.trim()) {
                    formData.append("instruct", customVoiceInstruct.value.trim());
                }
            } else if (modelTypeSelect.value === 'VoiceDesign') {
                formData.append("voice_design_prompt", voiceDesignPrompt.value);
            } else if (modelTypeSelect.value === 'Base') {
                formData.append("profile_id", savedVoiceSelect.value);
            }
            formData.append("temperature", temperatureSlider.value);

            const response = await fetch('/api/generate', {
                method: 'POST',
                body: formData,
                signal: activeAbortController.signal
            });

            if (!response.ok) {
                const errText = await response.text().catch(() => '');
                throw new Error(`API returned ${response.status}: ${errText.substring(0, 120)}`);
            }

            log(`[${index + 1}] Server responded OK — downloading audio blob...`);
            const blob = await response.blob();
            para.audioBlob = blob;

            if (para.audioUrl) URL.revokeObjectURL(para.audioUrl);

            para.audioUrl = URL.createObjectURL(blob);
            para.status = 'done';
            const genElapsed = ((performance.now() - genStartTime) / 1000).toFixed(1);
            const blobKB = (blob.size / 1024).toFixed(0);
            log(`[${index + 1}] Done — ${genElapsed}s round-trip, ${blobKB} KB`, 'ok');

            // Auto-save audio and project metadata
            autoSaveParagraphAudio(index, blob).catch(e => console.warn('Auto-save failed:', e));

        } catch (error) {
            const genElapsed = ((performance.now() - genStartTime) / 1000).toFixed(1);
            if (error.name === 'AbortError' || error === 'timeout') {
                const isTimeout = error === 'timeout' ||
                    (activeAbortController && activeAbortController.signal.reason === 'timeout');
                para.status = 'error';
                if (isTimeout) {
                    log(`[${index + 1}] Generation timed out after 30 min — check Activity Log and ~/.qwen_tts_studio/app.log`, 'error');
                } else {
                    para.status = 'idle';
                    log(`[${index + 1}] Generation aborted by user after ${genElapsed}s`, 'warn');
                }
            } else {
                console.error('Generation failed:', error);
                para.status = 'error';
                log(`[${index + 1}] FAILED after ${genElapsed}s — ${error.message}`, 'error');
            }
        } finally {
            clearTimeout(timeoutTimer);
            clearInterval(heartbeatInterval);
            activeAbortController = null;
            activeGenerationCount--;
            if (activeGenerationCount === 0) setGeneratingState(false);
        }

        updateCardUi(index);
        updateDownloadButtonVisibility();
    };

    function updateCardUi(index) {
        const para = paragraphsData[index];
        const statusBadge = document.getElementById(`status-${para.id}`);
        const btnGen = document.getElementById(`btn-gen-${para.id}`);
        const audioEl = document.getElementById(`audio-${para.id}`);

        if (statusBadge) {
            statusBadge.className = `status-badge ${para.status}`;
            statusBadge.textContent = getStatusText(para.status);
        }

        if (btnGen) {
            btnGen.disabled = (para.status === 'generating');
            btnGen.textContent = (para.status === 'done' || para.status === 'regenerate') ? 'Regenerate' : 'Generate';
        }

        if (audioEl) {
            if (para.audioUrl) {
                audioEl.src = para.audioUrl;
                audioEl.classList.remove('hidden');
            } else {
                audioEl.classList.add('hidden');
            }
        }
    }

    btnGenerateAll.addEventListener('click', async () => {
        const remaining = paragraphsData.filter(p => p.status !== 'done').length;
        const total = paragraphsData.length;
        log(`Batch generation starting — ${remaining} of ${total} paragraph(s) remaining`);
        await runGenerationPool();
        if (!generationStopped) {
            const doneCount = paragraphsData.filter(p => p.status === 'done').length;
            log(`Batch complete — ${doneCount}/${total} paragraphs generated successfully.`, 'ok');
        } else {
            const doneCount = paragraphsData.filter(p => p.status === 'done').length;
            log(`Batch stopped by user — ${doneCount}/${total} completed.`, 'warn');
        }
    });

    function updateDownloadButtonVisibility() {
        if (paragraphsData.length > 0) {
            downloadOptions.classList.remove('hidden');
            const allDone = paragraphsData.every(p => p.status === 'done');
            if (allDone) {
                btnDownloadAll.disabled = false;
                btnDownloadAll.classList.add('primary-btn');
                btnDownloadAll.classList.remove('secondary-btn');
                btnDownloadAll.style.opacity = '1';
                btnDownloadAll.title = '';
            } else {
                btnDownloadAll.disabled = true;
                btnDownloadAll.classList.remove('primary-btn');
                btnDownloadAll.classList.add('secondary-btn');
                btnDownloadAll.style.opacity = '0.5';
                btnDownloadAll.title = 'All paragraphs must be Ready to download';
            }
        } else {
            downloadOptions.classList.add('hidden');
        }
        updateChaptersButtonState();
    }

    function updateChaptersButtonState() {
        if (!btnCopyChapters) return;
        const allDone = paragraphsData.length > 0 && paragraphsData.every(p => p.status === 'done');
        const hasChapters = paragraphsData.some(p => p.chapter);
        const ready = allDone && hasChapters;
        btnCopyChapters.disabled = !ready;
        btnCopyChapters.style.opacity = ready ? '1' : '0.5';
        if (!hasChapters) {
            btnCopyChapters.title = 'Mark at least one paragraph as a chapter start';
        } else if (!allDone) {
            btnCopyChapters.title = 'All paragraphs must be Ready (generate audio first)';
        } else {
            btnCopyChapters.title = '';
        }
    }

    function chapterTitle(text) {
        const t = (text || '').trim();
        const dot = t.indexOf('.');
        return (dot === -1 ? t : t.slice(0, dot)).trim();
    }

    async function paragraphDuration(blob) {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        try {
            const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
            return decoded.duration; // seconds
        } finally {
            ctx.close();
        }
    }

    function copyToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text);
        }
        // Fallback for non-secure contexts
        return new Promise((resolve, reject) => {
            try {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                resolve();
            } catch (e) {
                reject(e);
            }
        });
    }

    btnCopyChapters.addEventListener('click', async () => {
        const originalText = btnCopyChapters.textContent;
        btnCopyChapters.disabled = true;
        btnCopyChapters.textContent = 'Building...';
        try {
            // Walk all paragraphs in order to compute cumulative start times,
            // matching the merge (1s of silence between every segment).
            const chapters = [];
            let cursor = 0; // seconds
            for (const para of paragraphsData) {
                if (para.chapter) {
                    chapters.push({ title: chapterTitle(para.text), start: Math.round(cursor) });
                }
                let blob = para.audioBlob;
                if (!blob && para.audioUrl) {
                    const r = await fetch(para.audioUrl);
                    if (r.ok) {
                        blob = await r.blob();
                        para.audioBlob = blob; // cache
                    }
                }
                if (blob) {
                    cursor += await paragraphDuration(blob);
                }
                cursor += 1.0; // inter-segment silence inserted by /api/merge
            }

            if (chapters.length === 0) {
                log('No chapters marked.', 'error');
                return;
            }

            await copyToClipboard(JSON.stringify(chapters, null, 2));
            log(`Chapters shortcode copied (${chapters.length} chapter(s)).`, 'ok');
            btnCopyChapters.textContent = 'Copied!';
            setTimeout(() => { btnCopyChapters.textContent = originalText; }, 1500);
        } catch (error) {
            console.error('Failed to build chapters shortcode:', error);
            log(`Copy chapters failed: ${error.message}`, 'error');
            btnCopyChapters.textContent = originalText;
        } finally {
            btnCopyChapters.disabled = false;
            updateChaptersButtonState();
        }
    });

    btnDownloadAll.addEventListener('click', async () => {
        // Collect blobs — fetch from server if blob was not cached in memory
        const blobsToMerge = (await Promise.all(
            paragraphsData
                .filter(p => p.status === 'done' && (p.audioBlob != null || p.audioUrl != null))
                .map(async p => {
                    if (p.audioBlob) return p.audioBlob;
                    if (p.audioUrl) {
                        const r = await fetch(p.audioUrl);
                        if (!r.ok) return null;
                        const b = await r.blob();
                        p.audioBlob = b; // cache it
                        return b;
                    }
                    return null;
                })
        )).filter(b => b != null);

        if (blobsToMerge.length === 0) {
            log("No audio generated yet!", 'error');
            return;
        }

        btnDownloadAll.disabled = true;
        const originalText = btnDownloadAll.textContent;
        btnDownloadAll.textContent = 'Processing...';

        try {
            log('Merging segments...');
            const formData = new FormData();
            blobsToMerge.forEach((blob, idx) => {
                formData.append('files', blob, `segment_${idx}.wav`);
            });

            const mergeResponse = await fetch('/api/merge', {
                method: 'POST',
                body: formData
            });

            if (!mergeResponse.ok) {
                throw new Error('Merge API failed: ' + mergeResponse.status);
            }

            let finalBlob = await mergeResponse.blob();
            log('Merge complete.', 'ok');

            // Always apply Clear Speech treatment
            log('Applying treatment...');
            btnDownloadAll.textContent = 'Applying Treatment...';
            const treatFormData = new FormData();
            treatFormData.append("audio_file", finalBlob, "merged.wav");
            treatFormData.append("treatment_type", "clear");

            const treatResponse = await fetch('/api/treat', {
                method: 'POST',
                body: treatFormData
            });

            if (treatResponse.ok) {
                finalBlob = await treatResponse.blob();
                log('Treatment applied.', 'ok');
            } else {
                log('Treatment failed. Using raw audio.', 'warn');
            }

            // Convert format if needed
            if (selectedFormat === 'm4a') {
                log('Converting to M4A...');
                btnDownloadAll.textContent = 'Converting...';
                const convertFormData = new FormData();
                convertFormData.append('audio_file', finalBlob, 'audio.wav');
                convertFormData.append('output_format', 'm4a');
                const convertRes = await fetch('/api/convert', { method: 'POST', body: convertFormData });
                if (convertRes.ok) {
                    finalBlob = await convertRes.blob();
                    log('Converted to M4A.', 'ok');
                } else {
                    log('M4A conversion failed. Downloading as WAV.', 'warn');
                }
            }

            const customTitle = projectNameInput.value.trim() || 'Qwen3_TTS';
            const safeTitle = customTitle.replace(/[^a-z0-9_ -]/gi, '_').replace(/\s+/g, '_');
            const ext = (selectedFormat === 'm4a' && finalBlob.type.includes('mp4')) ? 'm4a' : 'wav';

            const downloadUrl = URL.createObjectURL(finalBlob);
            const a = document.createElement('a');
            a.href = downloadUrl;
            a.download = `${safeTitle}.${ext}`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(downloadUrl);
            log('Download started.', 'ok');

        } catch (error) {
            console.error("Failed to merge:", error);
            log(`Download failed: ${error.message}`, 'error');
        }

        btnDownloadAll.disabled = false;
        btnDownloadAll.textContent = originalText;
    });

    function escapeHtml(unsafe) {
        return unsafe
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    // Progress bar elements
    const globalStatusBadge = document.getElementById('global-status-badge');

    // ─── Download Progress Banner ─────────────────────────────────────────────
    const downloadBanner = document.getElementById('download-progress-banner');
    const downloadBannerText = document.getElementById('download-progress-text');
    const downloadBannerDetail = document.getElementById('download-progress-detail');
    const downloadBannerFile = document.getElementById('download-progress-file');
    const downloadProgressFill = document.getElementById('download-progress-fill');
    let bannerFadeTimer = null;

    function formatBytes(bytes) {
        if (!bytes || bytes <= 0) return '';
        if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
        if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`;
        if (bytes >= 1e3) return `${(bytes / 1e3).toFixed(0)} KB`;
        return `${bytes} B`;
    }

    function formatEta(seconds) {
        if (!seconds || seconds <= 0) return '';
        if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
        if (seconds >= 60) return `${Math.floor(seconds / 60)} min`;
        return `${Math.round(seconds)}s`;
    }

    function formatElapsed(seconds) {
        if (!seconds || seconds <= 0) return '';
        if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
        if (seconds >= 60) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
        return `${seconds}s`;
    }

    function showDownloadBanner({ mainText, detailText, fileText, phase, pct }) {
        if (bannerFadeTimer) { clearTimeout(bannerFadeTimer); bannerFadeTimer = null; }
        downloadBanner.classList.remove('hidden', 'fading-out', 'stalled', 'connecting');
        if (phase === 'stalled') downloadBanner.classList.add('stalled');
        else if (phase === 'connecting') downloadBanner.classList.add('connecting');

        downloadBannerText.textContent = mainText;
        if (downloadBannerDetail) {
            downloadBannerDetail.textContent = detailText || '';
            downloadBannerDetail.style.display = detailText ? 'block' : 'none';
        }
        if (downloadBannerFile) {
            downloadBannerFile.textContent = fileText || '';
            downloadBannerFile.style.display = fileText ? 'block' : 'none';
        }
        if (downloadProgressFill && phase !== 'connecting') {
            // For determinate phases, JS-set width takes effect. For connecting,
            // the CSS rule applies `width: 35% !important` plus the indeterminate
            // animation, so we don't touch it here.
            const safePct = Math.max(0, Math.min(100, pct || 0));
            downloadProgressFill.style.width = `${safePct}%`;
        }
    }

    function hideDownloadBanner() {
        downloadBanner.classList.add('fading-out');
        bannerFadeTimer = setTimeout(() => {
            downloadBanner.classList.add('hidden');
            downloadBanner.classList.remove('fading-out', 'stalled', 'connecting');
            if (downloadProgressFill) downloadProgressFill.style.width = '0%';
        }, 400);
    }

    // Strip leading "(...)" annotations and any path components from tqdm's desc
    // so the banner shows just the filename being fetched.
    function shortFileLabel(desc) {
        if (!desc) return '';
        let s = String(desc).trim();
        // Drop a leading "(…)" prefix that huggingface_hub sometimes prepends.
        s = s.replace(/^\([^)]*\)\s*/, '');
        // Keep only the trailing path component
        const slash = s.lastIndexOf('/');
        if (slash >= 0) s = s.slice(slash + 1);
        // Truncate excessively long filenames
        if (s.length > 60) s = s.slice(0, 28) + '…' + s.slice(-28);
        return s;
    }

    // ─── Progress SSE (long-lived, auto-reconnects) ───────────────────────────
    // Design rationale: we keep ONE persistent EventSource rather than opening
    // a new one per generation. When the server closes it (e.g. after "ready"),
    // EventSource's built-in reconnect kicks in immediately so the next model
    // load is also covered.  The progressState object is read by the per-para
    // heartbeat inside generateSingle().
    const progressState = { status: 'idle', pct: 0, description: '' };

    (function connectProgressSSE() {
        const evtSource = new EventSource('/api/progress');
        // Track the previous status so we only log/announce on real transitions
        // (the server may re-send the same state after a reconnect).
        let prevStatus = null;

        evtSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                progressState.status = data.status || 'idle';
                progressState.pct = Math.floor(Math.max(0, Math.min(100, data.progress || 0)));
                progressState.description = data.description || '';

                const transitioned = data.status !== prevStatus;
                const isActive = data.status === 'downloading' || data.status === 'stalled';
                const isStalled = data.status === 'stalled';
                // phase is finer-grained than status; fall back to status if absent
                // (older server build) so the UI degrades gracefully.
                const phase = data.phase ||
                    (isStalled ? 'stalled'
                        : (data.bytes_done > 0 ? 'downloading' : 'connecting'));

                if (isActive) {
                    const repoShort = (data.repo_id || '').replace('Qwen/', '');
                    const pctStr = progressState.pct > 0 ? ` — ${progressState.pct}%` : '';
                    let mainText;
                    if (phase === 'connecting') {
                        mainText = repoShort
                            ? `Connecting to download ${repoShort}…`
                            : 'Connecting to Hugging Face…';
                    } else {
                        mainText = repoShort
                            ? `Downloading ${repoShort}${pctStr}`
                            : `Downloading model${pctStr}`;
                    }

                    // Build the detail line — keep bytes/rate/ETA visible during
                    // stalls so the user can see how far they got, and append a
                    // "no data for Ns" note instead of replacing everything.
                    let detail = '';
                    if (phase === 'connecting') {
                        const elapsed = formatElapsed(data.elapsed_seconds);
                        detail = elapsed
                            ? `Waiting for first byte · ${elapsed} elapsed`
                            : 'Waiting for first byte…';
                    } else if (data.bytes_done > 0) {
                        detail = formatBytes(data.bytes_done);
                        if (data.bytes_total > 0) detail += ` / ${formatBytes(data.bytes_total)}`;
                        if (data.rate_bps > 0) detail += ` · ${formatBytes(data.rate_bps)}/s`;
                        const eta = formatEta(data.eta_seconds);
                        if (eta) detail += ` · ~${eta} left`;
                        const elapsed = formatElapsed(data.elapsed_seconds);
                        if (elapsed) detail += ` · ${elapsed} elapsed`;
                        if (isStalled && data.idle_seconds > 0) {
                            detail += ` · no data for ${data.idle_seconds}s`;
                        }
                    }

                    const fileText = phase !== 'connecting'
                        ? shortFileLabel(data.current_file)
                        : '';

                    if (phase === 'stalled') {
                        updateStatusBadge('stalled', `Stalled ${progressState.pct}%`);
                    } else if (phase === 'connecting') {
                        updateStatusBadge('downloading', 'Connecting…');
                    } else {
                        updateStatusBadge('downloading', `Downloading… ${progressState.pct}%`);
                    }
                    showDownloadBanner({ mainText, detailText: detail, fileText, phase, pct: progressState.pct });
                } else if (data.status === 'ready') {
                    updateStatusBadge('ready', 'Model Ready');
                    if (transitioned && (prevStatus === 'downloading' || prevStatus === 'stalled')) {
                        log('Model initialized successfully.', 'ok');
                    }
                    hideDownloadBanner();
                } else if (data.status === 'error') {
                    updateStatusBadge('error', 'Model Error');
                    if (transitioned) {
                        console.error('Model Error:', data.description);
                        log(`Failed: ${data.description}`, 'error');
                    }
                    hideDownloadBanner();
                } else {
                    updateStatusBadge('idle', 'Model Status: Idle');
                }

                prevStatus = data.status;
            } catch (e) {
                console.error('Error parsing progress SSE:', e);
            }
        };

        evtSource.onerror = () => {
            // EventSource reconnects automatically; nothing to do
        };
    })();

    // --- Dynamic UI Logic ---
    function applyModelTypeConfig() {
        const selected = modelTypeSelect.value;
        configCustomVoice.classList.add('hidden');
        configCustomVoiceInstruct.classList.add('hidden');
        configVoiceDesign.classList.add('hidden');
        configBase.classList.add('hidden');

        if (selected === 'CustomVoice') {
            configCustomVoice.classList.remove('hidden');
            configCustomVoiceInstruct.classList.remove('hidden');
        } else if (selected === 'VoiceDesign') {
            configVoiceDesign.classList.remove('hidden');
        } else if (selected === 'Base') {
            configBase.classList.remove('hidden');
            loadProfiles();
        }
    }

    modelTypeSelect.addEventListener('change', applyModelTypeConfig);
    applyModelTypeConfig(); // run once on load to show Voice Cloning panel by default

    // --- Profile Management ---
    async function loadProfiles() {
        try {
            const res = await fetch('/api/profiles');
            const profiles = await res.json();

            // Rebuild select options
            savedVoiceSelect.innerHTML = '<option value="">-- Select a Voice Profile --</option>';
            profiles.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.id;
                opt.textContent = p.builtin ? `⭐ ${p.name}` : p.name;
                if (p.builtin) opt.dataset.builtin = 'true';
                savedVoiceSelect.appendChild(opt);
            });

            // Auto-select the built-in profile if nothing is selected
            if (!savedVoiceSelect.value) {
                const builtinOpt = savedVoiceSelect.querySelector('[data-builtin="true"]');
                if (builtinOpt) savedVoiceSelect.value = builtinOpt.value;
            }

            // Trigger change event to update delete button visibility
            savedVoiceSelect.dispatchEvent(new Event('change'));
        } catch (e) {
            console.error("Failed to load profiles", e);
        }
    }

    // Toggle delete button visibility (hide for built-in profiles)
    savedVoiceSelect.addEventListener('change', () => {
        const selectedOpt = savedVoiceSelect.options[savedVoiceSelect.selectedIndex];
        if (savedVoiceSelect.value && (!selectedOpt || !selectedOpt.dataset.builtin)) {
            btnDeleteProfile.classList.remove('hidden');
        } else {
            btnDeleteProfile.classList.add('hidden');
        }
    });

    btnDeleteProfile.addEventListener('click', async (e) => {
        e.preventDefault();
        const profileId = savedVoiceSelect.value;
        if (!profileId) return;

        const profileName = savedVoiceSelect.options[savedVoiceSelect.selectedIndex].text;
        if (!confirm(`Are you sure you want to delete the voice profile "${profileName}"?`)) {
            return;
        }

        try {
            const res = await fetch(`/api/profiles/${profileId}`, { method: 'DELETE' });
            if (!res.ok) throw new Error("Failed to delete profile");

            log(`Deleted voice profile: ${profileName}`);
            await loadProfiles();
        } catch (e) {
            log("Error deleting profile: " + e.message, 'error');
        }
    });

    btnOpenSaveModal.addEventListener('click', (e) => {
        e.preventDefault();
        saveProfileModal.classList.remove('hidden');
    });

    btnCloseModal.addEventListener('click', () => {
        saveProfileModal.classList.add('hidden');
    });

    btnSaveProfile.addEventListener('click', async () => {
        const name = newProfileName.value.trim();
        const text = newProfileText.value.trim();
        const file = newProfileAudio.files[0];

        if (!name || !text || !file) {
            log("Please fill in all fields (name, audio, and text) to save a profile.", 'error');
            return;
        }

        btnSaveProfile.disabled = true;
        btnSaveProfile.textContent = "Saving...";

        try {
            const formData = new FormData();
            formData.append('name', name);
            formData.append('ref_text', text);
            formData.append('ref_audio', file);

            const res = await fetch('/api/profiles', {
                method: 'POST',
                body: formData
            });

            if (!res.ok) throw new Error("Failed to save profile");

            const data = await res.json();
            await loadProfiles();
            savedVoiceSelect.value = data.id; // Auto-select the new profile
            savedVoiceSelect.dispatchEvent(new Event('change')); // trigger the hide/show logic

            // Reset modal and close
            newProfileName.value = '';
            newProfileText.value = '';
            newProfileAudio.value = '';
            saveProfileModal.classList.add('hidden');

        } catch (e) {
            log("Error saving profile: " + e.message, 'error');
        } finally {
            btnSaveProfile.disabled = false;
            btnSaveProfile.textContent = "Save Voice Profile";
        }
    });

    // Initial load
    loadProfiles();

    // --- Helper UI Functions ---
    function updateStatusBadge(statusClass, textContent) {
        if (!globalStatusBadge) return;
        globalStatusBadge.className = `global-status-badge ${statusClass}`;
        globalStatusBadge.textContent = textContent;
    }

    // Merge short consecutive paragraphs. Operates on { text, chapter, heading } items.
    // Any heading (H2) always starts a fresh paragraph (never glued onto the previous
    // one); following body text may still merge into it, and the resulting paragraph
    // keeps the heading's chapter flag.
    function combineShortParagraphs(items, minLen = 225, maxLen = 325) {
        const result = [];
        let buffer = null;
        for (const item of items) {
            if (!buffer) {
                buffer = { ...item };
                continue;
            }
            // A heading must begin its own paragraph (whether or not it's a chapter).
            if (item.heading) {
                result.push(buffer);
                buffer = { ...item };
                continue;
            }
            const combined = buffer.text + ' ' + item.text;
            if (buffer.text.length < minLen && combined.length <= maxLen) {
                buffer.text = combined; // buffer.chapter preserved
            } else {
                result.push(buffer);
                buffer = { ...item };
            }
        }
        if (buffer) result.push(buffer);
        return result;
    }

    // H2 headings whose text matches one of these are kept as paragraph breaks
    // but are NOT marked as chapters (boilerplate section headers).
    const CHAPTER_EXCLUDE = new Set([
        'settle in',
        'thought starter',
        'reflection questions',
        'humor break',
        'bring the inspiration with you',
    ]);

    // Normalize a heading to a comparison key: drop emojis/markdown/punctuation,
    // collapse whitespace, lowercase. e.g. "**🧘 Settle in**" -> "settle in".
    function headingKey(text) {
        return text
            .replace(/\p{Extended_Pictographic}/gu, '')
            .replace(/[\uFE0F\u200D\u20E3]/gu, "")
            .replace(/[^\p{L}\p{N}\s]/gu, '')
            .replace(/\s+/g, ' ')
            .trim()
            .toLowerCase();
    }

    // Strip Markdown markers from a single line so the TTS model reads the words,
    // not the syntax. Runs before cleanTextGeneral. Note: heading detection (## )
    // happens before this, so removing the markers here is safe.
    function stripMarkdown(line) {
        // Unescape backslash-escaped punctuation (e.g. "Jerusalem\!" -> "Jerusalem!")
        line = line.replace(/\\([\\!.,*_~`>#()\[\]-])/g, '$1');
        // Heading markers (#, ##, ### ...) and blockquote markers (>).
        // Match whether or not text follows, so a bare "###" line strips to empty.
        line = line.replace(/^#{1,6}(\s+|$)/, '');
        line = line.replace(/^>\s?/, '');
        // List bullets (-, *, +) and ordered-list markers (1. ) at the start
        line = line.replace(/^[-*+]\s+/, '');
        line = line.replace(/^\d+\.\s+/, '');
        // Links: [text](url) -> text
        line = line.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
        // Bold / italic emphasis
        line = line.replace(/\*\*([^*]+)\*\*/g, '$1');
        line = line.replace(/__([^_]+)__/g, '$1');
        line = line.replace(/\*([^*]+)\*/g, '$1');
        line = line.replace(/_([^_]+)_/g, '$1');
        // Any stray leftover emphasis markers
        line = line.replace(/\*\*/g, '');
        return line.trim();
    }

    // General cleanup applied to all text
    function cleanTextGeneral(text) {
        // strip emojis and pictographs — they confuse the TTS model
        text = text.replace(/\p{Extended_Pictographic}/gu, '');
        // strip leftover emoji modifiers: variation selector, ZWJ, keycap, skin tones
        text = text.replace(/[\uFE0F\u200D\u20E3]|\p{Emoji_Modifier}/gu, "");
        text = text.replace(/[ \t]+/g, ' ');

        // Ending every line with a period
        text = text.replace(/(^[^\n.]+)(?=$|\n)/gm, '$1.');

        // remove any lines with just a period and whitespace
        text = text.replace(/^\.\s*$/gm, '');

        // removes blank lines and whitespace
        text = text.replace(/\n+/g, '\n').trim();

        // sometimes brackets mess up tts
        text = text.replace(/[\[\]]/g, ',');

        return text;
    }

    // Bible-specific transforms — only applied when "Bible text formatting" is checked
    function cleanTextBible(text) {
        // Replace numbered Bible books
        text = text.replace(/\b1 (Corinthians)\b/g, 'First $1');
        text = text.replace(/\b2 (Corinthians)\b/g, 'Second $1');
        text = text.replace(/\b1 (Chronicles)\b/g, 'First $1');
        text = text.replace(/\b2 (Chronicles)\b/g, 'Second $1');
        text = text.replace(/\b1 (Kings)\b/g, 'First $1');
        text = text.replace(/\b2 (Kings)\b/g, 'Second $1');
        text = text.replace(/\b1 (Samuel)\b/g, 'First $1');
        text = text.replace(/\b2 (Samuel)\b/g, 'Second $1');
        text = text.replace(/\b1 (Thessalonians)\b/g, 'First $1');
        text = text.replace(/\b2 (Thessalonians)\b/g, 'Second $1');
        text = text.replace(/\b1 (Timothy)\b/g, 'First $1');
        text = text.replace(/\b2 (Timothy)\b/g, 'Second $1');
        text = text.replace(/\b1 (Peter)\b/g, 'First $1');
        text = text.replace(/\b2 (Peter)\b/g, 'Second $1');
        text = text.replace(/\b1 (John)\b/g, 'First $1');
        text = text.replace(/\b2 (John)\b/g, 'Second $1');
        text = text.replace(/\b3 (John)\b/g, 'Third $1');

        text = text.replace(/\bAMPC\b/g, 'Amplified Bible Classic.');
        text = text.replace(/\bAMP\b/g, 'Amplified Bible.');
        text = text.replace(/\bASV\b/g, 'American Standard Version.');
        text = text.replace(/\bCEB\b/g, 'Common English Bible.');
        text = text.replace(/\bCEV\b/g, 'Contemporary English Version.');
        text = text.replace(/\bCSB\b/g, 'Christian Standard Bible.');
        text = text.replace(/\bESV\b/g, 'English Standard Version.');
        text = text.replace(/\bGNT\b/g, 'Good News Translation.');
        text = text.replace(/\bHCSB\b/g, 'Holman Christian Standard Bible.');
        text = text.replace(/\bKJV\b/g, 'King James Version.');
        text = text.replace(/\bTLB\b/g, 'The Living Bible.');
        text = text.replace(/\bMSG\b/g, 'The Message.');
        text = text.replace(/\bNABRE\b/g, 'New American Bible Revised Edition.');
        text = text.replace(/\bNAB\b/g, 'New American Bible.');
        text = text.replace(/\bNASB\b/g, 'New American Standard Bible.');
        text = text.replace(/\bNCV\b/g, 'New Century Version.');
        text = text.replace(/\bNIRV\b/g, 'New International Reader\'s Version.');
        text = text.replace(/\bNIV\b/g, 'New International Version.');
        text = text.replace(/\bNJB\b/g, 'New Jerusalem Bible.');
        text = text.replace(/\bNKJV\b/g, 'New King James Version.');
        text = text.replace(/\bNLT\b/g, 'New Living Translation.');
        text = text.replace(/\bNRSV\b/g, 'New Revised Standard Version.');
        text = text.replace(/\bRSV\b/g, 'Revised Standard Version.');
        text = text.replace(/\bTPT\b/g, 'The Passion Translation.');
        text = text.replace(/\bWEB\b/g, 'World English Bible.');
        text = text.replace(/\bYLT\b/g, 'Young\'s Literal Translation.');
        text = text.replace(/\bERV\b/g, 'Easy to Read Version.');
        text = text.replace(/\bNIrV\b/g, 'New International Reader\'s Version.');

        // Verses and ranges formatting
        text = text.replace(/(\d+):(\d+)/g, '$1. verse $2,');
        text = text.replace(/[,.]-(\d+)/g, ' through $1.');
        text = text.replace(/\[(\d+)\]/g, '');
        // Replace colons not part of time notation
        text = text.replace(/(?<!\d):(?!\d)/g, ', ');

        return text;
    }

    // ─── Settings & Diagnostics Modal ─────────────────────────────────────────

    const settingsModal = document.getElementById('settings-modal');
    const btnOpenSettings = document.getElementById('btn-open-settings');
    const btnCloseSettingsModal = document.getElementById('btn-close-settings-modal');

    // Tab switching
    document.querySelectorAll('.settings-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.settings-tab-panel').forEach(p => p.classList.remove('active'));
            tab.classList.add('active');
            const panelId = `settings-tab-${tab.dataset.tab}`;
            const panel = document.getElementById(panelId);
            if (panel) panel.classList.add('active');
            // Lazy-load content when tab is first opened
            if (tab.dataset.tab === 'models') loadModelsTab();
            if (tab.dataset.tab === 'diagnostics') loadDiagnosticsTab();
        });
    });

    btnOpenSettings.addEventListener('click', () => {
        settingsModal.classList.remove('hidden');
        loadSettingsDefaults();
    });

    btnCloseSettingsModal.addEventListener('click', () => {
        settingsModal.classList.add('hidden');
    });

    // Close on backdrop click
    settingsModal.addEventListener('click', (e) => {
        if (e.target === settingsModal) settingsModal.classList.add('hidden');
    });

    // ─── Settings: Defaults tab ───────────────────────────────────────────────

    async function loadSettingsDefaults() {
        try {
            const res = await fetch('/api/settings');
            if (!res.ok) return;
            const data = await res.json();
            const sizeEl = document.getElementById('pref-model-size');
            const typeEl = document.getElementById('pref-model-type');
            const autoEl = document.getElementById('pref-auto-preload');
            if (sizeEl && data.preferred_model_size) sizeEl.value = data.preferred_model_size;
            if (typeEl && data.preferred_model_type) typeEl.value = data.preferred_model_type;
            if (autoEl && data.auto_preload_on_start != null) autoEl.checked = !!data.auto_preload_on_start;
        } catch (e) {
            // Settings endpoint not yet available — ignore silently
        }
    }

    document.getElementById('btn-save-settings').addEventListener('click', async () => {
        const msgEl = document.getElementById('settings-save-msg');
        msgEl.className = 'settings-save-msg';
        msgEl.classList.remove('hidden');
        msgEl.textContent = 'Saving…';
        try {
            const payload = {
                preferred_model_size: document.getElementById('pref-model-size').value,
                preferred_model_type: document.getElementById('pref-model-type').value,
                auto_preload_on_start: document.getElementById('pref-auto-preload').checked
            };
            const res = await fetch('/api/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            msgEl.textContent = 'Saved!';
            msgEl.classList.add('ok');
            setTimeout(() => { msgEl.classList.add('hidden'); }, 2000);
        } catch (e) {
            msgEl.textContent = `Error: ${e.message}`;
            msgEl.classList.add('error');
        }
    });

    // ─── Settings: Models tab ─────────────────────────────────────────────────

    // Tracks which model rows are currently downloading (by "size|type" key)
    const modelDownloadingKeys = new Set();

    async function loadModelsTab() {
        const wrap = document.getElementById('models-table-wrap');
        if (!wrap) return;
        wrap.innerHTML = '<p class="hint">Loading…</p>';
        try {
            const res = await fetch('/api/models/status');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            renderModelsTable(data);
        } catch (e) {
            wrap.innerHTML = `<p class="hint" style="color:#fc8181;">Failed to load models: ${escapeHtml(e.message)}</p>`;
        }
    }

    document.getElementById('btn-refresh-models').addEventListener('click', loadModelsTab);

    const MODEL_TYPE_LABELS = {
        'Base': 'Voice Cloning',
        'CustomVoice': 'Preprogrammed Voice',
        'VoiceDesign': 'Voice Design',
    };

    function renderModelsTable(data) {
        const wrap = document.getElementById('models-table-wrap');
        if (!wrap) return;
        const { models = [], loaded = null } = data;
        if (models.length === 0) {
            wrap.innerHTML = '<p class="hint">No model variants found.</p>';
            return;
        }
        const table = document.createElement('table');
        table.className = 'models-table';
        table.innerHTML = `
            <thead>
                <tr>
                    <th>Size</th><th>Mode</th><th>Cached</th><th>Size</th><th>Actions</th>
                </tr>
            </thead>
            <tbody id="models-tbody"></tbody>`;
        wrap.innerHTML = '';
        wrap.appendChild(table);
        const tbody = document.getElementById('models-tbody');

        models.forEach(m => {
            const key = `${m.size}|${m.type}`;
            const isLoaded = loaded && loaded === m.id;
            const isDownloading = modelDownloadingKeys.has(key);
            const row = document.createElement('tr');

            const sizeDisplay = m.size_mb ? `${(m.size_mb / 1024).toFixed(1)} GB` : '—';
            const modeLabel = MODEL_TYPE_LABELS[m.type] || m.type;

            let actionCell = '';
            if (isDownloading) {
                actionCell = `<span class="model-dl-progress" id="model-dl-${m.size}-${m.type}">Downloading…</span>`;
            } else if (m.cached) {
                actionCell = `<button class="secondary-btn small-btn model-action-btn danger-btn"
                    onclick="modelDelete('${escapeHtml(m.size)}','${escapeHtml(m.type)}')">Delete</button>`;
            } else if (m.needs_repair) {
                actionCell = `<button class="secondary-btn small-btn model-action-btn" style="color:#f6c90e;border-color:rgba(246,201,14,0.3);"
                    onclick="modelRepair('${escapeHtml(m.size)}','${escapeHtml(m.type)}')">Repair</button>`;
            } else {
                actionCell = `<button class="secondary-btn small-btn model-action-btn"
                    onclick="modelDownload('${escapeHtml(m.size)}','${escapeHtml(m.type)}')">Download</button>`;
            }

            row.innerHTML = `
                <td>${escapeHtml(m.size)}</td>
                <td>${escapeHtml(modeLabel)}${isLoaded ? '<span class="model-loaded-badge">LOADED</span>' : ''}</td>
                <td class="${m.cached ? 'model-cached-yes' : 'model-cached-no'}">${m.cached ? '&#10003;' : '&#10007;'}</td>
                <td>${sizeDisplay}</td>
                <td>${actionCell}</td>`;
            tbody.appendChild(row);
        });
    }

    window.modelDownload = async (size, type) => {
        const key = `${size}|${type}`;
        modelDownloadingKeys.add(key);
        // Re-render to show spinner
        try {
            const res = await fetch('/api/models/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ size, type })
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            log(`Download started for ${size} ${type} (id: ${data.model_id})`, 'ok');
            // Poll /api/progress for completion; when done, refresh the table
            const pollInterval = setInterval(async () => {
                const done = progressState.status === 'ready' || progressState.status === 'error';
                if (done) {
                    clearInterval(pollInterval);
                    modelDownloadingKeys.delete(key);
                    loadModelsTab();
                }
            }, 1500);
        } catch (e) {
            log(`Failed to start model download: ${e.message}`, 'error');
            modelDownloadingKeys.delete(key);
        }
        loadModelsTab();
    };

    window.modelRepair = async (size, type) => {
        if (!confirm(`Wipe the incomplete download for ${size} ${type}?\n\nThis removes partial files so you can re-download cleanly.`)) return;
        try {
            const res = await fetch(`/api/models/${encodeURIComponent(size)}/${encodeURIComponent(type)}/repair`, {
                method: 'POST'
            });
            if (res.status === 409) {
                alert('Cannot repair the currently loaded model — swap to a different model first.');
                return;
            }
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const freed = data.freed_mb ? ` (freed ${(data.freed_mb / 1024).toFixed(1)} GB)` : '';
            log(`Repaired model cache for ${size} ${type}${freed}`, 'ok');
            loadModelsTab();
        } catch (e) {
            log(`Failed to repair model: ${e.message}`, 'error');
        }
    };

    window.modelDelete = async (size, type) => {
        if (!confirm(`Delete the ${size} ${type} model from disk? This cannot be undone.`)) return;
        try {
            const res = await fetch(`/api/models/${encodeURIComponent(size)}/${encodeURIComponent(type)}`, {
                method: 'DELETE'
            });
            if (res.status === 409) {
                alert('Cannot delete the currently loaded model — swap to another first.');
                return;
            }
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const freed = data.freed_mb ? ` (freed ${(data.freed_mb / 1024).toFixed(1)} GB)` : '';
            log(`Deleted model ${size} ${type}${freed}`, 'ok');
            loadModelsTab();
        } catch (e) {
            log(`Failed to delete model: ${e.message}`, 'error');
        }
    };

    // ─── Settings: Diagnostics tab ────────────────────────────────────────────

    let lastDiagJson = null;

    async function loadDiagnosticsTab() {
        const healthRows = document.getElementById('diag-health-rows');
        const jsonPre = document.getElementById('diag-json-pre');
        if (!healthRows || !jsonPre) return;
        healthRows.innerHTML = '';
        jsonPre.textContent = 'Loading…';
        try {
            const res = await fetch('/api/diag');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            lastDiagJson = data;
            jsonPre.textContent = JSON.stringify(data, null, 2);
            renderDiagRows(data, healthRows);
        } catch (e) {
            jsonPre.textContent = `Error: ${e.message}`;
        }
    }

    function renderDiagRows(data, container) {
        const p = data.platform || {};
        const t = data.torch || {};
        const arch = p.machine || '—';
        const macos = p.mac_ver || p.release || '—';
        const mps = !!t.mps_available;
        const rows = [
            { label: 'Architecture', value: arch, cls: '' },
            { label: 'macOS', value: macos, cls: '' },
            { label: 'Torch / MPS available', value: mps ? '✓' : '✗', cls: mps ? 'ok' : 'error' },
            { label: 'HF reachable', value: data.hf_reachable ? '✓' : '✗', cls: data.hf_reachable ? 'ok' : 'error' },
            { label: 'Disk free', value: data.disk_free_gb != null ? `${data.disk_free_gb.toFixed(1)} GB` : '—', cls: '' },
            { label: 'Loaded model', value: data.loaded_model || 'None', cls: '' },
            { label: 'Log file', value: data.log_file || '~/.qwen_tts_studio/app.log', cls: '' },
        ];
        rows.forEach(r => {
            const row = document.createElement('div');
            row.className = 'diag-row';
            row.innerHTML = `<span class="diag-row-label">${escapeHtml(r.label)}</span>
                             <span class="diag-row-value ${r.cls}">${escapeHtml(String(r.value))}</span>`;
            container.appendChild(row);
        });
    }

    document.getElementById('btn-refresh-diag').addEventListener('click', loadDiagnosticsTab);

    document.getElementById('btn-copy-diag').addEventListener('click', () => {
        const text = lastDiagJson ? JSON.stringify(lastDiagJson, null, 2) : (document.getElementById('diag-json-pre') || {}).textContent || '';
        navigator.clipboard.writeText(text).then(() => {
            const btn = document.getElementById('btn-copy-diag');
            const orig = btn.textContent;
            btn.textContent = 'Copied!';
            setTimeout(() => { btn.textContent = orig; }, 1500);
        }).catch(() => {});
    });
});
