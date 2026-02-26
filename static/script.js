document.addEventListener('DOMContentLoaded', () => {
    const textInput = document.getElementById('text-input');
    const btnParse = document.getElementById('btn-parse');
    const paragraphsContainer = document.getElementById('paragraphs-container');
    const paragraphsList = document.getElementById('paragraphs-list');
    const paraCountSpan = document.getElementById('para-count');
    const btnGenerateAll = document.getElementById('btn-generate-all');
    const btnDownloadAll = document.getElementById('btn-download-all');
    const modelTypeSelect = document.getElementById('model-type-select');
    const speakerSelect = document.getElementById('speaker-select');
    const voiceDesignPrompt = document.getElementById('voice-design-prompt');
    const refAudioUpload = document.getElementById('ref-audio-upload');
    const refTextInput = document.getElementById('ref-text-input');
    const downloadTitleInput = document.getElementById('download-title-input');

    // Auto-Updater
    const updateBanner = document.getElementById('update-banner');
    const updateVersionSpan = document.getElementById('update-version');
    const btnDoUpdate = document.getElementById('btn-do-update');
    let otaDownloadUrl = null;

    const downloadOptions = document.getElementById('download-options');

    // --- Activity Log ---
    const logEntries = document.getElementById('activity-log-entries');
    const btnMinimizeLog = document.getElementById('btn-minimize-log');
    let logMinimized = false;

    function log(msg, level = 'info') {
        const entry = document.createElement('div');
        entry.className = `log-entry ${level}`;
        const now = new Date();
        const time = now.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const timeSpan = document.createElement('span');
        timeSpan.className = 'log-time';
        timeSpan.textContent = time;
        const msgSpan = document.createElement('span');
        msgSpan.className = 'log-msg';
        msgSpan.textContent = msg;
        entry.appendChild(timeSpan);
        entry.appendChild(msgSpan);
        logEntries.appendChild(entry);
        logEntries.scrollTop = logEntries.scrollHeight;
    }

    btnMinimizeLog.addEventListener('click', () => {
        logMinimized = !logMinimized;
        logEntries.style.display = logMinimized ? 'none' : '';
        btnMinimizeLog.textContent = logMinimized ? '+' : '−';
        document.getElementById('activity-log').style.height = logMinimized ? 'auto' : '';
    });

    log('Studio ready.', 'ok');

    const configCustomVoice = document.getElementById('config-custom-voice');
    const configVoiceDesign = document.getElementById('config-voice-design');
    const configBase = document.getElementById('config-base');

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

    // Progress bar elements
    const progressPanel = document.getElementById('download-progress-panel');
    const statusText = document.getElementById('model-status-text');
    const statusDesc = document.getElementById('model-status-desc');
    const progressBarFill = document.getElementById('progress-bar-fill');

    let paragraphsData = [];

    // Shared concurrency pool — keeps up to 3 requests in-flight at once.
    // On MPS the backend serializes them; on CPU/CUDA they run truly parallel.
    async function runGenerationPool() {
        const CONCURRENCY = 3;
        const queue = paragraphsData
            .map((p, i) => i)
            .filter(i => paragraphsData[i].status !== 'done');

        function next() {
            if (queue.length === 0) return Promise.resolve();
            const i = queue.shift();
            return window.generateSingle(i).finally(() => next());
        }

        const workers = Array.from({ length: Math.min(CONCURRENCY, queue.length) }, next);
        await Promise.all(workers);
    }

    // Parse text area into paragraphs, then immediately start generation
    btnParse.addEventListener('click', async () => {
        let text = textInput.value.trim();
        if (!text) return;

        text = cleanText(text);

        // Split by newlines, filter empty
        const rawParagraphs = text.split(/\n+/).map(p => p.trim()).filter(p => p.length > 0);

        paragraphsData = rawParagraphs.map((text, index) => ({
            id: `para-${index}`,
            text: text,
            status: 'idle',
            audioBlob: null,
            audioUrl: null
        }));

        renderParagraphs();

        paragraphsContainer.classList.remove('hidden');
        paraCountSpan.textContent = paragraphsData.length;
        updateDownloadButtonVisibility();
        log(`Parsed ${paragraphsData.length} paragraph(s) — starting generation...`);

        // Auto-start generation immediately
        btnGenerateAll.disabled = true;
        btnGenerateAll.textContent = 'Generating All...';
        await runGenerationPool();
        btnGenerateAll.disabled = false;
        btnGenerateAll.textContent = 'Generate All';
        log('All done.', 'ok');
    });

    function renderParagraphs() {
        paragraphsList.innerHTML = '';

        paragraphsData.forEach((para, index) => {
            const card = document.createElement('div');
            card.className = 'paragraph-card';
            card.id = para.id;

            card.innerHTML = `
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
        });
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
            if (para.status === 'done') {
                para.status = 'regenerate';
                updateCardUi(index);
                updateDownloadButtonVisibility();
            }
        }
    };

    // Expose to window for the inline onclick handlers
    window.generateSingle = async (index) => {
        const para = paragraphsData[index];
        if (para.status === 'generating') return;

        para.status = 'generating';
        if (para.audioUrl) URL.revokeObjectURL(para.audioUrl);
        para.audioBlob = null;
        para.audioUrl = null;
        updateCardUi(index);
        log(`Generating para ${index + 1}...`);

        try {
            const formData = new FormData();
            formData.append("text", para.text);
            formData.append("language", "English");
            formData.append("model_size", "1.7B");
            formData.append("model_type", modelTypeSelect.value);

            if (modelTypeSelect.value === 'CustomVoice') {
                formData.append("speaker", speakerSelect.value);
            } else if (modelTypeSelect.value === 'VoiceDesign') {
                formData.append("voice_design_prompt", voiceDesignPrompt.value);
            } else if (modelTypeSelect.value === 'Base') {
                if (!savedVoiceSelect.value) {
                    alert("Please select a saved voice profile first.");
                    para.status = 'idle';
                    updateCardUi(index);
                    return;
                }
                formData.append("profile_id", savedVoiceSelect.value);
            }

            const response = await fetch('/api/generate', {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                throw new Error('API returned ' + response.status);
            }

            const blob = await response.blob();
            para.audioBlob = blob;

            if (para.audioUrl) URL.revokeObjectURL(para.audioUrl);

            para.audioUrl = URL.createObjectURL(blob);
            para.status = 'done';
            log(`Para ${index + 1} ready.`, 'ok');

        } catch (error) {
            console.error('Generation failed:', error);
            para.status = 'error';
            log(`Para ${index + 1} failed.`, 'error');
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
        btnGenerateAll.disabled = true;
        btnGenerateAll.textContent = 'Generating All...';
        log(`Generating remaining paragraph(s)...`);

        await runGenerationPool();

        btnGenerateAll.disabled = false;
        btnGenerateAll.textContent = 'Generate All';
        log('All done.', 'ok');
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
    }

    btnDownloadAll.addEventListener('click', async () => {
        const blobsToMerge = paragraphsData
            .filter(p => p.status === 'done' && p.audioBlob != null)
            .map(p => p.audioBlob);

        if (blobsToMerge.length === 0) {
            alert("No audio generated yet!");
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

            const customTitle = downloadTitleInput.value.trim() || 'Qwen3_TTS';
            // sanitize the filename
            const safeTitle = customTitle.replace(/[^a-z0-9_ -]/gi, '_').replace(/\s+/g, '_');

            const downloadUrl = URL.createObjectURL(finalBlob);
            const a = document.createElement('a');
            a.href = downloadUrl;
            a.download = `${safeTitle}.wav`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(downloadUrl);
            log('Download started.', 'ok');

        } catch (error) {
            console.error("Failed to merge:", error);
            log(`Download failed: ${error.message}`, 'error');
            alert("Failed to merge audio.");
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

    // --- Dynamic UI Logic ---
    function applyModelTypeConfig() {
        const selected = modelTypeSelect.value;
        configCustomVoice.classList.add('hidden');
        configVoiceDesign.classList.add('hidden');
        configBase.classList.add('hidden');

        if (selected === 'CustomVoice') {
            configCustomVoice.classList.remove('hidden');
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
            alert("Error deleting profile: " + e.message);
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
            alert("Please fill in all fields to save a profile.");
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
            alert("Error saving profile: " + e.message);
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

    // Connect to SSE for progress updates
    const evtSource = new EventSource("/api/progress");
    let isFinished = false;

    evtSource.onmessage = (event) => {
        if (isFinished) return;

        try {
            const data = JSON.parse(event.data);

            if (data.status === 'downloading') {
                const pct = Math.floor(Math.max(0, Math.min(100, data.progress)));
                updateStatusBadge('downloading', `Downloading Model... ${pct}%`);
            } else if (data.status === 'ready') {
                isFinished = true;
                updateStatusBadge('ready', 'Model Ready');
                evtSource.close();
            } else if (data.status === 'error') {
                isFinished = true;
                updateStatusBadge('error', 'Model Error');
                console.error("Model Error:", data.description);
                evtSource.close();
            } else {
                updateStatusBadge('idle', 'Model Status: Idle');
            }

        } catch (e) {
            console.error("Error parsing progress SSE:", e);
        }
    };

    evtSource.onerror = () => {
        // Server closed the SSE connection — stop reconnecting
        isFinished = true;
        evtSource.close();
    };

    function cleanText(text) {
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
        // Replace colons not part of time notation (e.g. 3:00) — digit:digit already handled above
        text = text.replace(/(?<!\d):(?!\d)/g, ', ');

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
});
