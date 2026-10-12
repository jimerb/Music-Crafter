'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
  const energyNames = ['', 'Gentle', 'Easygoing', 'Lively', 'Energetic'];
  const iconPaths = {
    'audio-lines': '<path d="M3 10v4m4-7v10m5-14v18m5-15v12m4-9v6"/>',
    library: '<path d="M4 4h4v16H4zM10 4h4v16h-4zM17 4l4-1 4 16-4 1z"/>',
    star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z"/>',
    disc: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2"/><path d="M7 7h.01M17 17h.01"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    'chevron-right': '<path d="m9 5 7 7-7 7"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    settings: '<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="10" cy="18" r="2"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.2 8a7 7 0 0 1 11.5-3L20 9M4 15l3.3 4a7 7 0 0 0 11.5-3"/>',
    play: '<path d="m8 4 12 8-12 8z"/>',
    pause: '<path d="M7 4h3v16H7zM14 4h3v16h-3z"/>',
    'skip-back': '<path d="M5 4v16m14-16L7 12l12 8z"/>',
    volume: '<path d="m11 4-5 4H3v8h3l5 4zM15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/>',
    sparkles: '<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4zM20 3v4m-2-2h4"/>',
    x: '<path d="m6 6 12 12M6 18 18 6"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    plus: '<path d="M12 4v16M4 12h16"/>',
  };
  const icon = name => `<svg class="mc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name] || iconPaths.disc}</svg>`;
  document.querySelectorAll('[data-icon]').forEach(node => { node.innerHTML = icon(node.dataset.icon); });

  const state = {
    library: {collections: [], tracks: [], roots: [], capabilities: {}},
    settings: {favorites: [], notes: {}, defaultDuration: 60, exportFormat: 'wav', bitrate: 320},
    clips: [], trackId: null, collectionId: null, view: 'all', tab: 'details',
    query: '', mood: '', energy: '', sort: 'title',
    duration: 60, variation: 'auto', tempo: 0, pitch: 0, loop: false,
    clip: null, activeRequest: null, busy: false, scanning: false, generatingTrack: null,
    progress: 0, jobMessage: '', notice: '', error: '', durationError: '',
    modal: null, modalData: {}, loaded: false,
    variationsLoading: false, variationsKey: '', variationsAvailable: {}, variationsError: false,
    scan: null, scanRequestPending: false, scanConnectionError: '', scanWarningsOpen: false,
    trackPreview: {id: null, title: '', loading: false, error: ''},
  };
  const audio = $('#clip-audio');
  const previewAudio = $('#track-preview-audio');
  audio.volume = .7;
  previewAudio.volume = .7;
  let settingsQueue = Promise.resolve();
  let notesTimer;
  let toastTimer;
  let modalPreviousFocus;
  let variationsTimer;
  let variationsRevision = 0;
  let scanTimer;
  let scanRevision = 0;
  let completedScanId;
  let previewRevision = 0;

  const values = value => Array.isArray(value) ? value : value ? [value] : [];
  const track = () => state.library.tracks.find(item => String(item.id) === state.trackId)
    || (state.busy && state.generatingTrack?.id === state.trackId ? state.generatingTrack : undefined);
  const favorite = id => state.settings.favorites.some(value => String(value) === String(id));
  function formatTime(number, fractional = false) {
    number = Math.max(0, Number(number) || 0);
    const minutes = Math.floor(number / 60);
    const seconds = number - minutes * 60;
    const rounded = fractional ? Math.round(seconds * 1000) / 1000 : Math.floor(seconds);
    if (rounded >= 60) return `${minutes + 1}:00`;
    return `${minutes}:${String(rounded).padStart(rounded < 10 ? 2 + (String(rounded).includes('.') ? String(rounded).split('.')[1].length + 1 : 0) : 2, '0')}`;
  }
  function parseDuration(value) {
    const input = String(value).trim();
    if (!/^\d+(?::\d{1,2}){0,2}$/.test(input)) return NaN;
    const parts = input.split(':').map(Number);
    if (parts.length === 1) return parts[0];
    if (parts.slice(1).some(part => part >= 60)) return NaN;
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    return NaN;
  }
  function safeDuration(value) { return Number.isFinite(value) && value >= 4 && value <= 7200; }
  function editableDuration(value) { return formatTime(Math.round(value)); }
  function validClockEdit(value) { return /^\d*(?::\d{0,2}){0,2}$/.test(value) && value.length <= 9; }
  function request() {
    return {trackId: state.trackId, duration: state.duration, variation: state.variation, tempo: state.tempo, pitch: state.pitch, loop: state.loop};
  }
  function sameRequest(left, right) {
    return left && right && String(left.trackId) === String(right.trackId) && String(left.variation) === String(right.variation)
      && Math.abs(Number(left.duration) - Number(right.duration)) < .0001
      && Math.abs(Number(left.tempo) - Number(right.tempo)) < .05
      && Number(left.pitch || 0) === Number(right.pitch || 0) && Boolean(left.loop) === Boolean(right.loop);
  }
  const playable = () => Boolean(state.clip && !state.busy && !state.durationError && sameRequest(request(), state.activeRequest));
  function toast(message) {
    clearTimeout(toastTimer);
    $('#toast').textContent = message;
    $('#toast').hidden = false;
    toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 5000);
  }
  function globalError(message) {
    $('#global-error').innerHTML = `${esc(message)}<button data-action="retry">Retry</button>`;
    $('#global-error').hidden = !message;
  }
  async function api(url, options = {}) {
    let response;
    try { response = await fetch(url, {...options, headers: {'Content-Type': 'application/json', ...options.headers}}); }
    catch { throw new Error('Music Crafter is not responding. Check that the app is still running, then try again.'); }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.error === 'string' ? body.error : body.error?.message || body.message || `The request could not be completed (${response.status}).`);
    return body;
  }
  function persistSettings(patch) {
    const snapshot = JSON.parse(JSON.stringify(patch));
    settingsQueue = settingsQueue.catch(() => {}).then(() => api('/api/settings', {method: 'PATCH', body: JSON.stringify(snapshot)}));
    settingsQueue.catch(error => toast(`Could not save changes: ${error.message}`));
    return settingsQueue;
  }
  function flushNotes() {
    if (notesTimer) { clearTimeout(notesTimer); notesTimer = undefined; return persistSettings({notes: state.settings.notes}); }
    return Promise.resolve();
  }
  function variationMinimum(variation, selected = track()) {
    return Number(variation.minDuration || 0) / ((state.tempo || selected?.bpm || 1) / (selected?.bpm || 1));
  }
  function variationMaximum(variation, selected = track()) {
    return variation.maxDuration == null ? Infinity : Number(variation.maxDuration) / ((state.tempo || selected?.bpm || 1) / (selected?.bpm || 1));
  }
  const variationKey = () => `${state.trackId}|${state.duration}|${state.tempo}`;
  function variationAvailability(variation) {
    if (state.variationsKey === variationKey()) return state.variationsAvailable[String(variation.id)];
    return null;
  }
  function validateVariation() {
    const selected = track();
    const variation = selected?.variations?.find(item => String(item.id) === state.variation);
    if (state.variation !== 'auto' && (!variation || state.duration + .001 < variationMinimum(variation) || state.duration - .001 > variationMaximum(variation) || variationAvailability(variation)?.available === false)) {
      state.variation = 'auto';
      syncTrackPreview();
      state.notice = 'Auto selected to fit this length. Named variations have their own musical length ranges.';
      return state.notice;
    }
    return '';
  }
  function refreshVariationAvailability(immediate = false) {
    clearTimeout(variationsTimer);
    const revision = ++variationsRevision;
    const selected = track();
    if (!selected || state.durationError) return;
    const key = variationKey();
    const parameters = new URLSearchParams({trackId: selected.id, duration: String(state.duration), tempo: String(state.tempo)});
    state.variationsLoading = true; state.variationsError = false;
    updateVariationControls();
    variationsTimer = setTimeout(async () => {
      try {
        const response = await api(`/api/variations?${parameters}`);
        if (revision !== variationsRevision) return;
        const items = response.variations || response;
        state.variationsAvailable = Array.isArray(items) ? Object.fromEntries(items.map(item => [String(item.id), item])) : items;
        state.variationsKey = key; state.variationsLoading = false;
        const notice = validateVariation();
        if (notice) { state.notice = notice; audio.pause(); renderPreview(); renderCraft(); }
        updateVariationControls();
        if (notice && $('.mc-notice')) $('.mc-notice').textContent = notice;
      } catch {
        if (revision !== variationsRevision) return;
        state.variationsLoading = false; state.variationsError = true;
        if (state.variation !== 'auto') { state.variation = 'auto'; syncTrackPreview(); state.notice = 'Auto selected. Named variation availability could not be checked.'; audio.pause(); renderPreview(); renderCraft(); }
        updateVariationControls();
      }
    }, immediate ? 0 : 250);
  }
  function setDirty(notice = '', renderControls = true) {
    audio.pause();
    syncTrackPreview();
    state.error = '';
    state.notice = notice || 'Ready to generate with your new settings.';
    if (renderControls) renderCraft();
    else updateDurationControls();
    renderTrackList();
    renderPreview();
  }
  function selectTrack(id) {
    if (state.busy || String(id) === state.trackId) return;
    flushNotes();
    audio.pause();
    if (String(id) !== state.trackPreview.id) closeTrackPreview(false);
    state.trackId = String(id);
    const selected = track();
    state.variation = 'auto'; state.tempo = selected?.bpm || 0; state.pitch = 0; state.loop = false;
    state.durationError = ''; state.error = ''; state.notice = 'Choose a length and variation to craft this track.';
    renderAll();
    refreshVariationAvailability(true);
  }
  function normalizeLibrary(library) {
    library.collections ||= [];
    library.tracks ||= [];
    library.roots ||= [];
    library.capabilities ||= {};
    library.tracks = library.tracks.map(item => ({...item, id: String(item.id), collectionId: String(item.collectionId), bpm: Number(item.bpm) || 0,
      instruments: values(item.instruments), moods: values(item.moods), style: values(item.style),
      variations: values(item.variations).map(variation => typeof variation === 'object' ? {...variation, id: String(variation.id)} : {id: String(variation), name: String(variation)})}));
    return library;
  }
  async function load() {
    globalError('');
    try {
      const [library, settings, clips, scan] = await Promise.all([api('/api/library'), api('/api/settings'), api('/api/clips'), api('/api/scan').catch(() => null)]);
      state.library = normalizeLibrary(library);
      state.settings = {...state.settings, ...settings};
      state.settings.favorites ||= []; state.settings.notes ||= {};
      state.clips = clips.clips || [];
      state.duration = safeDuration(Number(state.settings.defaultDuration)) ? Number(state.settings.defaultDuration) : 60;
      const initial = state.library.tracks.find(item => item.title === 'Tasty Jazz') || state.library.tracks[0];
      state.trackId = initial?.id || null; state.tempo = initial?.bpm || 0;
      state.collectionId = state.library.collections.length === 1 ? String(state.library.collections[0].id) : null;
      state.loaded = true;
      state.notice = 'Choose a length, then generate your first clip.';
      renderAll();
      refreshVariationAvailability(true);
      if (scan || library.scan) applyScan(scan || library.scan, true, true);
    } catch (error) {
      globalError(error.message);
      $('#library-status').textContent = 'Connection unavailable';
      $('#track-list').innerHTML = '<p class="mc-empty">Your library could not be loaded.</p>';
      renderCraft();
    }
  }

  function filteredTracks() {
    const query = state.query.toLowerCase();
    return state.library.tracks.filter(item => (!state.collectionId || item.collectionId === state.collectionId)
      && (state.view !== 'favorites' || favorite(item.id))
      && (!state.energy || Number(item.energy) === Number(state.energy))
      && (!state.mood || item.moods.includes(state.mood))
      && (!query || [item.title, item.album, item.description, ...item.instruments, ...item.moods, ...item.style].join(' ').toLowerCase().includes(query)))
      .sort((left, right) => state.sort === 'tempo' ? Number(left.bpm) - Number(right.bpm) || left.title.localeCompare(right.title)
        : state.sort === 'energy' ? Number(left.energy) - Number(right.energy) || left.title.localeCompare(right.title) : left.title.localeCompare(right.title));
  }
  function renderNavigation() {
    $('#all-count').textContent = state.library.tracks.length;
    $('#favorite-count').textContent = state.library.tracks.filter(item => favorite(item.id)).length;
    $('#clip-count').textContent = state.clips.length;
    $('#library-status').textContent = state.scanning ? scanHeadline() : `${state.library.tracks.length} tracks · ${state.library.collections.length} ${state.library.collections.length === 1 ? 'collection' : 'collections'}`;
    $('#source-summary').textContent = `${state.library.roots.length} ${state.library.roots.length === 1 ? 'folder' : 'folders'} · ${state.library.collections.length} ${state.library.collections.length === 1 ? 'collection' : 'collections'}`;
    for (const action of ['all', 'favorites', 'clips']) {
      $(`.mc-sidebar [data-action="${action}"]`).setAttribute('aria-pressed', String(state.view === action && (action !== 'all' || !state.collectionId)));
    }
    const collectionScroll = $('#collections').scrollTop;
    $('#collections').innerHTML = state.library.collections.map(item => `<button data-action="collection" data-id="${esc(item.id)}" aria-pressed="${state.view !== 'clips' && String(item.id) === state.collectionId}">${icon('disc')}<span class="mc-collection-label">${esc(item.name)}</span><span class="mc-nav-count">${Number(item.trackCount) || 0}</span></button>`).join('') || '<p class="mc-small mc-muted">Add a music folder to start.</p>';
    $('#collections').scrollTop = collectionScroll;
    const collection = state.library.collections.find(item => String(item.id) === state.collectionId);
    $('#library-heading').textContent = state.view === 'clips' ? 'My clips' : state.view === 'favorites' ? 'Favorites' : collection?.name || 'All tracks';
    $('#library-subtitle').textContent = state.view === 'clips' ? 'Your generated arrangements, ready to play and export.' : state.view === 'favorites' ? 'The music you want to come back to.' : collection ? `${collection.trackCount} tracks · Ready to arrange` : 'Find a track. Choose a length. Make it yours.';
    $('#track-browser').hidden = state.view === 'clips';
    $('#clips-browser').hidden = state.view !== 'clips';
    $('[data-action="rescan"]').disabled = state.scanning || state.scanRequestPending;
  }
  function renderFilters() {
    const moods = [...new Set(state.library.tracks.flatMap(item => item.moods))].sort();
    $('#mood-filter').innerHTML = '<option value="">All moods</option>' + moods.map(mood => `<option value="${esc(mood)}" ${mood === state.mood ? 'selected' : ''}>${esc(mood)}</option>`).join('');
    $('#track-query').value = state.query;
    $('#energy-filter').value = state.energy;
    $('#sort-tracks').value = state.sort;
  }
  function renderTrackList() {
    const items = filteredTracks();
    if (state.trackPreview.id && (state.view === 'clips' || !items.some(item => item.id === state.trackPreview.id))) closeTrackPreview(false);
    const container = $('#track-list');
    const scroll = container.scrollTop;
    const focused = container.contains(document.activeElement) ? document.activeElement : null;
    const focusAction = focused?.dataset.action, focusValue = focused?.dataset.value, focusId = focused?.id;
    container.innerHTML = items.length ? items.map(item => `<div class="mc-track" data-track-id="${esc(item.id)}" data-selected="${item.id === state.trackId}" data-preview="${item.id === state.trackPreview.id}"><div class="mc-track-main"><button class="mc-track-select" data-action="select" data-id="${esc(item.id)}" aria-label="Select ${esc(item.title)}" aria-pressed="${item.id === state.trackId}" ${state.busy ? 'disabled' : ''}><span class="mc-track-name"><span class="mc-title">${esc(item.title)}</span>${!state.collectionId ? `<span class="mc-track-subtitle">${esc(item.album)}</span>` : ''}</span></button><div class="mc-preview-slot">${rowPreviewMarkup(item)}</div><span class="mc-bpm">${Number(item.bpm) || '—'}</span><span class="mc-energy" aria-label="${energyNames[Number(item.energy)] || 'Unrated'}">${[1,2,3,4].map(number => `<span class="${number <= Number(item.energy) ? 'mc-on' : ''}"></span>`).join('')}</span><button class="mc-fav" data-action="favorite" data-id="${esc(item.id)}" aria-label="${favorite(item.id) ? 'Unfavorite' : 'Favorite'} ${esc(item.title)}" aria-pressed="${favorite(item.id)}">${icon('star')}</button></div>${item.id === state.trackId ? rowVariationsMarkup(item) : ''}</div>`).join('') : `<div class="mc-empty-state"><h3>${state.library.tracks.length ? 'No matching tracks' : state.scanning ? 'Discovering your music' : 'Your library starts here'}</h3><p>${state.library.tracks.length ? 'Try another mood, energy level, or search.' : state.scanning ? 'Collections will appear when the scan finishes. You can close Settings while it works.' : 'Add a parent folder containing your music collections.'}</p>${state.library.tracks.length ? '<button class="mc-section" data-action="clear-filters">Clear filters</button>' : '<button class="mc-section" data-action="folders">Manage music folders</button>'}</div>`;
    container.scrollTop = scroll;
    if (focused) {
      const replacement = focusId ? document.getElementById(focusId) : [...container.querySelectorAll('[data-action]')].find(node => node.dataset.action === focusAction && node.dataset.value === focusValue && node.dataset.id === focused.dataset.id);
      replacement?.focus({preventScroll:true});
    }
    $('#track-count').textContent = `${items.length} of ${state.library.tracks.length} tracks`;
    renderTrackPreview();
  }
  function rowPreviewMarkup(item) {
    if (state.trackPreview.id !== item.id) return `<button class="mc-track-play" data-action="track-preview" data-id="${esc(item.id)}" aria-label="Preview ${esc(item.title)}" title="Listen to ${esc(item.title)}">${icon('play')}</button>`;
    return `<div class="mc-inline-preview" role="group" aria-label="Preview controls for ${esc(item.title)}"><div class="mc-inline-transport"><button data-action="track-preview" data-id="${esc(item.id)}" aria-label="Play preview of ${esc(item.title)}">${icon('play')}</button><button data-action="track-stop" aria-label="Restart track preview" title="Return to start">${icon('skip-back')}</button><input id="track-preview-seek" type="range" min="0" max="1000" value="0" aria-label="Track preview position"><output id="track-preview-time">0:00 / 0:30</output><label class="mc-inline-volume">${icon('volume')}<input id="track-preview-volume" type="range" min="0" max="100" value="${Math.round(previewAudio.volume * 100)}" aria-label="Track preview volume"></label><button data-action="track-close" aria-label="Close track preview" title="Close preview">${icon('x')}</button></div><span id="track-preview-message" class="mc-inline-message" role="status" aria-live="polite"></span></div>`;
  }
  function variationState(item, selected = track()) {
    const minimum = variationMinimum(item, selected), maximum = variationMaximum(item, selected);
    const tooShort = state.duration + .001 < minimum, tooLong = state.duration - .001 > maximum;
    const availability = variationAvailability(item);
    return {unavailable: tooShort || tooLong || availability?.available === false || state.variationsError,
      reason: tooShort ? `Needs ${formatTime(Math.ceil(minimum), true)}+` : tooLong ? `Up to ${formatTime(Math.floor(maximum), true)}` : availability?.available === false ? 'Try another length' : state.variationsError ? 'Availability unavailable' : ''};
  }
  function rowVariationsMarkup(selected) {
    const options = [{id:'auto',name:'Auto',auto:true}, ...selected.variations.filter(item => item.id !== 'auto')];
    return `<div class="mc-track-variations" role="group" aria-label="Variations for ${esc(selected.title)}"><div class="mc-variation-heading"><span>Variations</span><span>${state.variationsLoading ? 'Checking this length…' : `For ${formatTime(state.duration, true)}`}</span></div><div class="mc-variation-options">${options.map(item => {
      const status = item.auto ? {unavailable:false,reason:'Best fit for this length'} : variationState(item, selected);
      return `<button class="mc-variation-choice" data-action="variation" data-value="${esc(item.id)}" aria-pressed="${item.id === state.variation}" ${state.busy || !item.auto && (state.variationsLoading || status.unavailable) ? 'disabled' : ''} title="${esc(status.reason || `Use ${item.name}`)}"><span class="mc-variation-dot" aria-hidden="true"></span><span><span class="mc-variation-name">${esc(item.name)}</span>${status.reason ? `<small>${esc(status.reason)}</small>` : ''}</span></button>`;
    }).join('')}</div></div>`;
  }
  function renderTrackPreview() {
    const preview = state.trackPreview;
    const panel = $('.mc-inline-preview');
    if (!panel || !preview.id) return;
    const playing = !previewAudio.paused && !previewAudio.ended;
    const button = $('[data-action="track-preview"],[data-action="track-pause"]', panel);
    // Keep the clicked node and its icon stable during timer updates. Replacing
    // the SVG between pointer-down and pointer-up can swallow a human click.
    if (button.dataset.playing !== String(playing)) {
      button.dataset.playing = String(playing);
      button.dataset.action = playing ? 'track-pause' : 'track-preview';
      button.innerHTML = icon(playing ? 'pause' : 'play');
      button.setAttribute('aria-label', `${playing ? 'Pause' : 'Play'} preview of ${preview.title}`);
    }
    const message = $('#track-preview-message');
    const previewMessage = preview.error || (preview.loading ? 'Loading preview…' : `${preview.variationName || 'Original recording'} · 30-second excerpt`);
    if (message.textContent !== previewMessage) message.textContent = previewMessage;
    message.classList.toggle('mc-error', Boolean(preview.error));
    const duration = Number.isFinite(previewAudio.duration) ? previewAudio.duration : 0;
    $('#track-preview-time').textContent = `${formatTime(previewAudio.currentTime)} / ${duration ? formatTime(duration) : '0:30'}`;
    const seek = $('#track-preview-seek');
    seek.disabled = !duration;
    seek.value = duration ? Math.round(previewAudio.currentTime/duration*1000) : 0;
    seek.setAttribute('aria-valuetext', `${formatTime(previewAudio.currentTime)} of ${formatTime(duration)}`);
    $('#track-preview-volume').value = Math.round(previewAudio.volume*100);
  }
  function syncTrackPreview() {
    if (state.trackPreview.id !== state.trackId || !previewAudio.src || state.trackPreview.variation === state.variation) return;
    const keepPlaying = !previewAudio.paused;
    previewTrack(state.trackId, true, keepPlaying);
  }
  async function previewTrack(identifier, replay = false, play = true) {
    const selected = state.library.tracks.find(item => item.id === String(identifier));
    if (!selected) { toast('This track is no longer in the library. Rescan its folder to preview it.'); return; }
    audio.pause();
    const variation = state.trackId === selected.id ? state.variation : 'auto';
    const existing = state.trackPreview.id === selected.id && state.trackPreview.variation === variation && previewAudio.src && !state.trackPreview.error;
    if (existing && !replay && !previewAudio.paused) { previewAudio.pause(); return; }
    if (!state.busy && state.trackId !== selected.id) selectTrack(selected.id);
    const revision = ++previewRevision;
    if (!existing) {
      previewAudio.pause();
      const variationName = selected.variations.find(item => item.id === variation)?.name || 'Original recording';
      state.trackPreview = {id: selected.id, title: selected.title, variation, variationName, loading: true, error: ''};
      previewAudio.src = `/api/tracks/${encodeURIComponent(selected.id)}/preview.wav?${new URLSearchParams({variation})}`;
      previewAudio.load();
    } else if (replay || previewAudio.ended) previewAudio.currentTime = 0;
    renderTrackPreview(); renderTrackList();
    try { if (play) await previewAudio.play(); }
    catch (error) {
      if (revision !== previewRevision || error.name === 'AbortError') return;
      state.trackPreview.loading = false;
      state.trackPreview.error = 'This recording could not be previewed. Check its music folder, then try replaying.';
      renderTrackPreview(); renderTrackList();
    }
  }
  function stopTrackPreview() {
    previewAudio.pause();
    previewAudio.currentTime = 0;
    renderTrackPreview(); renderTrackList();
  }
  function pauseTrackPreview() {
    ++previewRevision;
    previewAudio.pause();
    renderTrackPreview();
  }
  function closeTrackPreview(render = true) {
    ++previewRevision;
    state.trackPreview = {id:null,title:'',loading:false,error:''};
    previewAudio.pause();
    previewAudio.removeAttribute('src');
    previewAudio.load();
    if (render) renderTrackList();
  }
  function renderCraft() {
    const selected = track();
    if (!selected) {
      $('#craft-panel').innerHTML = '<div class="mc-eyebrow">Craft a clip</div><p class="mc-empty-state">Select a track from your library to get started.</p>';
      return;
    }
    const capability = state.library.capabilities;
    const disabled = state.busy ? 'disabled' : '';
    const variationName = selected.variations.find(item => item.id === state.variation)?.name || 'Auto';
    const tempoText = !selected.bpm ? 'No fixed tempo listed' : `${Number(state.tempo.toFixed(1))} BPM${state.tempo === Number(selected.bpm) && !state.pitch ? ' · Original' : `${state.pitch ? ` · ${state.pitch > 0 ? '+' : ''}${state.pitch} st` : ''}`}`;
    $('#craft-panel').innerHTML = `<div class="mc-eyebrow">Craft a clip</div><div class="mc-craft-title"><h2>${esc(selected.title)}</h2><button class="mc-fav mc-ghost" data-action="favorite" data-id="${esc(selected.id)}" aria-label="${favorite(selected.id) ? 'Unfavorite' : 'Favorite'} selected track" aria-pressed="${favorite(selected.id)}">${icon('star')}</button></div><p class="mc-muted mc-small">${selected.bpm ? `${esc(selected.bpm)} BPM` : 'No fixed tempo listed'} · ${energyNames[Number(selected.energy)] || 'Music'} · ${esc(selected.style[0] || selected.album)}</p><p class="mc-chosen-variation">${icon('audio-lines')}<span>${esc(variationName)} variation</span></p>
      <div class="mc-section"><div class="mc-label-row"><label for="clip-length">Clip length</label><button data-action="original" ${disabled}>Use original</button></div><div class="mc-duration"><input id="clip-length" type="text" aria-label="Clip length in minutes and seconds" value="${editableDuration(state.duration)}" inputmode="numeric" pattern="[0-9:]+" maxlength="9" autocomplete="off" spellcheck="false" ${disabled} ${state.durationError ? 'aria-invalid="true"' : ''} aria-describedby="duration-help"><span>min:sec</span></div><div class="mc-presets">${[15,30,60,90].map(number => `<button data-action="preset" data-value="${number}" aria-pressed="${state.duration === number}" ${disabled}>${number < 60 ? `${number}s` : `${number / 60} min`}</button>`).join('')}</div><p id="duration-help" class="mc-form-hint">4 seconds to 2 hours · minutes:seconds or hours:minutes:seconds</p></div>
      <div class="mc-tempo-row"><div class="mc-tempo-summary"><span class="mc-muted mc-small">Tempo &amp; pitch</span><span>${esc(tempoText)}</span></div><button data-action="tempo" ${state.busy || !selected.bpm || !capability.tempo && !capability.pitch ? 'disabled' : ''}>${icon('settings')}Adjust</button></div>
      <div class="mc-options"><label><input id="clip-loop" type="checkbox" ${state.loop ? 'checked' : ''} ${state.busy || !capability.loop ? 'disabled' : ''}>Looping version</label>${!capability.loop ? '<span class="mc-disabled-reason">Planned</span>' : '<span class="mc-small mc-muted">For repeat playback</span>'}</div>
      <button data-action="generate" class="mc-primary mc-generate" ${state.busy || state.durationError || state.variationsLoading && state.variation !== 'auto' ? 'disabled' : ''}>${icon(state.busy ? 'audio-lines' : 'sparkles')}${state.busy ? 'Crafting your clip…' : 'Generate clip'}</button>
      ${state.busy ? `<div class="mc-progress" role="progressbar" aria-label="Clip generation" aria-valuenow="${state.progress}" aria-valuemin="0" aria-valuemax="100"><div style="width:${state.progress}%"></div></div>` : ''}
      <div class="mc-notice ${state.error || state.durationError ? 'mc-error' : ''}" role="${state.error || state.durationError ? 'alert' : 'status'}" aria-live="polite">${esc(state.durationError || state.error || (state.busy ? `${Math.round(state.progress)}% · ${state.jobMessage || 'Preparing music'}` : state.notice))}</div>`;
  }
  function updateVariationControls() {
    if (!track()) return;
    renderTrackList();
    if ($('[data-action="generate"]')) $('[data-action="generate"]').disabled = Boolean(state.busy || state.durationError || state.variationsLoading && state.variation !== 'auto');
  }
  function updateDurationControls() {
    $('#clip-length').value = editableDuration(state.duration);
    $('#clip-length').setAttribute('aria-invalid', String(Boolean(state.durationError)));
    updateVariationControls();
    document.querySelectorAll('[data-action="preset"]').forEach(button => button.setAttribute('aria-pressed', String(state.duration === Number(button.dataset.value))));
    $('[data-action="generate"]').disabled = Boolean(state.busy || state.durationError || state.variationsLoading && state.variation !== 'auto');
    $('.mc-notice').textContent = state.durationError || state.error || state.notice;
    $('.mc-notice').classList.toggle('mc-error', Boolean(state.durationError || state.error));
  }
  function renderMetadata() {
    const selected = track();
    $('.mc-tabs').querySelectorAll('button').forEach(button => button.setAttribute('aria-selected', String(button.dataset.value === state.tab)));
    $('#track-info').setAttribute('aria-labelledby', `tab-${state.tab}`);
    if (!selected) { $('#track-info').innerHTML = ''; return; }
    const tags = (items, action) => items.map(item => action ? `<button class="mc-tag-button" data-action="tag" data-value="${esc(item)}">${esc(item)}</button>` : `<span class="mc-tag">${esc(item)}</span>`).join('');
    if (state.tab === 'details') {
      $('#track-info').innerHTML = `<p class="mc-description">${esc(selected.description || 'No description available.')}</p>${selected.moods.length ? `<span class="mc-eyebrow">Mood &amp; character</span><div class="mc-tags">${tags(selected.moods, true)}</div>` : ''}${selected.instruments.length ? `<div class="mc-section"><span class="mc-eyebrow">Instruments</span><div class="mc-tags">${tags(selected.instruments.filter(item => item !== 'Instrumental'), false)}</div></div>` : ''}<div class="mc-details-line"><span>${esc(selected.album)}</span><span>${Number(selected.sampleRate) / 1000 || '—'} kHz · ${selected.channels === 2 ? 'Stereo' : selected.channels === 1 ? 'Mono' : `${selected.channels || '—'} channels`}</span>${selected.style.length ? `<span>${esc(selected.style.join(', '))}</span>` : ''}</div>`;
    } else if (state.tab === 'credits') {
      const credits = values(selected.credits);
      $('#track-info').innerHTML = `<dl class="mc-credits">${credits.map(item => {const name = item.name || [item.FirstName || item.firstName, item.LastName || item.lastName].filter(Boolean).join(' '); const percent = item.Percent ?? item.percent; return `<dt>${esc(item.Type || item.type || item.role || 'Credit')}</dt><dd>${esc(name || item.publisher || 'Unknown')}<br><span class="mc-muted mc-small">${esc(item.Afil || item.affiliation || '')}${percent != null ? ` · ${esc(percent)}%` : ''}</span></dd>`;}).join('')}<dt>Collection</dt><dd>${esc(selected.album)}</dd>${selected.source ? `<dt>Source</dt><dd>${esc(selected.source)}</dd>` : ''}</dl>${!credits.length ? '<p class="mc-description">No composer or publisher information available.</p>' : ''}`;
    } else {
      $('#track-info').innerHTML = `<label class="mc-small mc-muted" for="track-notes">Your notes</label><textarea id="track-notes" data-track-id="${esc(selected.id)}" placeholder="A good fit for…">${esc(state.settings.notes[selected.id] || '')}</textarea><p class="mc-form-hint">Saved automatically with this track.</p>`;
    }
  }
  function clipNote(clip) {
    const diagnostics = clip?.diagnostics;
    if (!diagnostics) return '';
    if (typeof diagnostics === 'string') return diagnostics;
    if (Array.isArray(diagnostics)) return diagnostics.filter(item => typeof item === 'string').join(' ');
    const message = diagnostics.userMessage || diagnostics.note || diagnostics.message;
    if (message) return message;
    if (diagnostics.short_form || diagnostics.shortForm || diagnostics.shortform) return 'Compact ending: this very short clip fits a whole musical finish.';
    return values(diagnostics.warnings).join(' ');
  }
  function timingNote(clip) {
    if (Math.abs(Number(clip?.diagnostics?.timingAdjustmentPercent) || 0) <= 2 || !Number(clip?.estimatedTempo)) return '';
    return `Length fitting: approx. ${Math.round(clip.estimatedTempo * 10) / 10} BPM (requested ${clip.tempo || clip.request?.tempo}).`;
  }
  function renderPreview() {
    const clip = state.clip;
    const ready = playable();
    $('#preview-title').innerHTML = clip ? `<h3>${esc(clip.title)}</h3><span class="mc-badge">${esc(clip.variation || 'Auto')}</span>${clip.loop ? '<span class="mc-badge">Loop</span>' : ''}` : '<h3>Your clip</h3>';
    $('#preview-status').textContent = state.busy ? 'Creating your arrangement…' : clip ? ready ? `${formatTime(clip.duration, true)} · Ready to play` : 'Settings changed · Generate to update' : 'Generate a clip to preview it';
    $('#preview-status').classList.toggle('mc-state-label', ready);
    const amplitudes = clip ? values(clip.waveform) : [];
    const svg = $('#waveform svg');
    svg.innerHTML = amplitudes.map((amplitude, index) => {const height = Math.max(.5, Math.min(1, Number(amplitude) || 0) * 27); const step = 600 / amplitudes.length; return `<rect x="${index * step}" y="${29 - height}" width="${Math.max(.4, step * .58)}" height="${height * 2}" rx="1" fill="${ready ? '#65dfc4' : '#526669'}"/>`;}).join('');
    $('#waveform-empty').hidden = Boolean(amplitudes.length);
    $('#waveform-empty').textContent = state.busy ? 'Finding a musical path to your length…' : 'Your arrangement will appear here';
    $('#waveform').setAttribute('aria-disabled', String(!ready));
    $('#waveform').tabIndex = ready ? 0 : -1;
    $('#playback-cursor').hidden = !clip;
    $('#waveform-ticks').innerHTML = clip ? [0,.25,.5,.75,1].map(fraction => `<span>${formatTime(clip.duration * fraction)}</span>`).join('') : '<span>0:00</span><span>—</span>';
    $('#arrangement').innerHTML = values(clip?.sections).map((section, index, items) => `<span class="${index === items.length - 1 && !clip.loop ? 'mc-section-end' : 'mc-section-loop'}" style="flex:${Math.max(.01, Number(section.duration) || 1)}" title="${esc(section.name)} · ${formatTime(section.start, true)}–${formatTime(Number(section.start) + Number(section.duration), true)}">${esc(section.name)}</span>`).join('');
    $('#play-button').disabled = !ready;
    $('#stop-button').disabled = !ready;
    $('#export-button').disabled = !ready;
    $('#preview-notice').textContent = clip ? `${!ready && !state.busy ? 'Generate again to hear your current length, variation, tempo, or pitch. ' : ''}${[clipNote(clip), timingNote(clip)].filter(Boolean).join(' ')}` : 'A complete musical beginning and ending, arranged to your chosen length.';
    updateTransport();
  }
  function updateTransport() {
    const current = audio.currentTime || 0;
    const duration = Number(state.clip?.duration) || 0;
    $('#playback-time').textContent = `${formatTime(current)} / ${formatTime(duration)}`;
    const percentage = duration ? Math.min(100, current / duration * 100) : 0;
    $('#playback-cursor').style.left = `${percentage}%`;
    $('#waveform').setAttribute('aria-valuenow', String(Math.round(percentage)));
    $('#waveform').setAttribute('aria-valuetext', `${formatTime(current)} of ${formatTime(duration)}`);
    const playing = !audio.paused && !audio.ended;
    $('#play-button').innerHTML = icon(playing ? 'pause' : 'play');
    $('#play-button').setAttribute('aria-label', playing ? 'Pause clip' : 'Play clip');
  }
  function renderClips() {
    $('#clips-browser').innerHTML = state.clips.length ? `<div class="mc-clips-list">${state.clips.map(clip => `<article class="mc-clip-card" data-selected="${clip.id === state.clip?.id}"><div class="mc-clip-content"><h3>${esc(clip.title)}</h3><p>${formatTime(clip.duration, true)} · ${esc(clip.variation || 'Auto')}${clip.tempo ? ` · ${esc(clip.tempo)} BPM` : ''}${clip.pitch ? ` · ${clip.pitch > 0 ? '+' : ''}${clip.pitch} st` : ''}${clip.loop ? ' · Loop' : ''}</p><p>${Number(clip.sampleRate) / 1000 || '—'} kHz · ${clip.channels === 2 ? 'Stereo' : 'Mono'}${clip.createdAt ? ` · ${esc(new Date(clip.createdAt).toLocaleDateString())}` : ''}</p></div><button data-action="open-clip" data-id="${esc(clip.id)}" ${state.busy ? 'disabled' : ''}>Open ${icon('chevron-right')}</button></article>`).join('')}</div>` : '<div class="mc-empty-state"><h3>Your next soundtrack starts here</h3><p>Generate a clip from your library. It will be saved here for playback and export.</p><button class="mc-section" data-action="all">Browse music</button></div>';
  }
  function renderAll() { renderNavigation(); renderFilters(); renderTrackList(); renderCraft(); renderMetadata(); renderPreview(); renderClips(); renderTrackPreview(); renderScanStatus(); }

  async function generate() {
    if (!track() || state.busy || state.durationError || state.variationsLoading && state.variation !== 'auto') return;
    audio.pause();
    const generatedRequest = request();
    state.generatingTrack = track();
    state.busy = true; state.progress = 2; state.error = ''; state.notice = ''; state.jobMessage = 'Starting your clip';
    renderAll();
    try {
      const job = await api('/api/generate', {method: 'POST', body: JSON.stringify(generatedRequest)});
      if (!job.id) throw new Error('The app did not return a clip job. Please try again.');
      state.jobMessage = 'Queued for generation'; renderCraft();
      let failures = 0;
      while (true) {
        await new Promise(resolve => setTimeout(resolve, 450));
        let update;
        try { update = await api(`/api/jobs/${encodeURIComponent(job.id)}`); failures = 0; }
        catch (error) {
          failures++;
          state.jobMessage = 'Reconnecting to your clip…'; renderCraft();
          if (failures >= 8) throw new Error(`${error.message} Your clip may still be finishing. Check My clips when the connection returns.`);
          continue;
        }
        const progress = Number(update.progress) || 0;
        state.progress = Math.max(state.progress, Math.min(100, progress <= 1 ? progress * 100 : progress));
        state.jobMessage = update.message || 'Arranging musical sections';
        renderCraft();
        if (update.status === 'error') throw new Error(typeof update.error === 'string' ? update.error : update.message || 'This arrangement could not be created. Try Auto or a longer length.');
        if (update.status === 'complete') {
          if (!update.clip?.playbackUrl) throw new Error('The clip finished, but its audio was not returned.');
          state.clip = update.clip;
          state.activeRequest = update.clip.request || generatedRequest;
          audio.src = update.clip.playbackUrl;
          audio.load();
          state.clips = [update.clip, ...state.clips.filter(clip => clip.id !== update.clip.id)];
          state.notice = `Your ${formatTime(update.clip.duration, true)} clip is ready. Play it or export.`;
          break;
        }
      }
    } catch (error) { state.error = error.message; }
    finally { state.busy = false; state.generatingTrack = null; renderAll(); }
  }
  function openClip(id) {
    if (state.busy) return;
    const clip = state.clips.find(item => String(item.id) === String(id));
    if (!clip) return;
    flushNotes(); audio.pause();
    state.trackId = String(clip.trackId);
    const selected = track();
    if (selected) {
      state.view = 'all'; state.collectionId = selected.collectionId;
      state.query = ''; state.mood = ''; state.energy = '';
    }
    const saved = clip.request || {trackId: clip.trackId, duration: clip.duration, variation: clip.variationId || selected?.variations?.find(item => item.name === clip.variation)?.id || 'auto', tempo: clip.tempo || selected?.bpm || 0, pitch: clip.pitch || 0, loop: Boolean(clip.loop)};
    state.duration = Number(saved.duration); state.variation = String(saved.variation || 'auto'); state.tempo = Number(saved.tempo); state.pitch = Number(saved.pitch || 0); state.loop = Boolean(saved.loop);
    state.clip = clip; state.activeRequest = {...saved, trackId: String(saved.trackId)};
    state.durationError = ''; state.error = ''; state.notice = 'Saved clip ready to play or export.';
    audio.src = clip.playbackUrl; audio.load();
    renderAll();
    refreshVariationAvailability(true);
    $('#preview-title').scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }

  function modal(title, body, actions = '') {
    if (!$('#modal-host').firstElementChild) modalPreviousFocus = document.activeElement;
    $('#modal-host').innerHTML = `<div class="mc-overlay"><section class="mc-dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title"><div class="mc-dialog-heading"><h2 id="dialog-title">${esc(title)}</h2><button class="mc-ghost" data-action="close" aria-label="Close dialog">${icon('x')}</button></div>${body}<div id="dialog-error" class="mc-dialog-error" role="alert"></div>${actions}</section></div>`;
    $('.mc-dialog button').focus();
  }
  function closeModal() {
    if (state.modalData.exporting || state.modalData.saving) return;
    $('#modal-host').innerHTML = ''; state.modal = null; state.modalData = {};
    if (modalPreviousFocus?.isConnected) modalPreviousFocus.focus();
  }
  function dialogError(message) { if ($('#dialog-error')) $('#dialog-error').textContent = message; }
  function formatOptions(value) {
    return `<option value="wav" ${value === 'wav' ? 'selected' : ''}>WAV · lossless</option><option value="mp3" ${value === 'mp3' ? 'selected' : ''} ${!state.library.capabilities.mp3 ? 'disabled' : ''}>MP3${!state.library.capabilities.mp3 ? ' · unavailable' : ' · compact'}</option>`;
  }
  const bitrateOptions = value => [128,192,256,320].map(rate => `<option value="${rate}" ${Number(value) === rate ? 'selected' : ''}>${rate} kbps${rate === 320 ? ' · High quality' : ''}</option>`).join('');
  function exportDialog() {
    if (!playable()) return;
    state.modal = 'export';
    const clip = state.clip;
    state.modalData = {format: state.settings.exportFormat === 'mp3' && state.library.capabilities.mp3 ? 'mp3' : 'wav', bitrate: state.settings.bitrate || 320, rate: 'original', bitDepth: 16, filename: `${clip.title.replace(/[<>:"/\\|?*]/g, '_')}_${Number(clip.duration.toFixed(3))}s`};
    renderExport();
  }
  function renderExport() {
    const data = state.modalData;
    const clip = state.clip;
    const mp3 = data.format === 'mp3';
    modal('Export clip', `<div class="mc-clip-summary">${esc(clip.title)} · ${formatTime(clip.duration, true)} · ${esc(clip.variation || 'Auto')}<br>${Number(clip.sampleRate) / 1000} kHz · ${clip.channels === 2 ? 'Stereo' : 'Mono'}</div><label>File name<input id="export-filename" value="${esc(data.filename)}" aria-label="Export file name" maxlength="180"></label><p class="mc-form-hint">.${data.format} is added automatically.</p><div class="mc-dialog-fields mc-section"><label>Format<select id="export-format" aria-label="Export format">${formatOptions(data.format)}</select></label>${mp3 ? `<label>MP3 quality<select id="export-bitrate" aria-label="MP3 bitrate">${bitrateOptions(data.bitrate)}</select></label>` : `<label>Sample rate<select id="export-rate" aria-label="Export sample rate"><option value="original" ${data.rate === 'original' ? 'selected' : ''}>Original · ${Number(clip.sampleRate) / 1000} kHz</option>${[44100,48000,96000].filter(rate => rate !== Number(clip.sampleRate)).map(rate => `<option value="${rate}" ${String(data.rate) === String(rate) ? 'selected' : ''}>${rate / 1000} kHz</option>`).join('')}</select></label><label>Bit depth<select id="export-depth" aria-label="Export bit depth">${[16,24,32].map(depth => `<option value="${depth}" ${Number(data.bitDepth) === depth ? 'selected' : ''}>${depth}-bit${depth === 16 ? ' · Original precision' : ''}</option>`).join('')}</select></label>`}</div><p id="export-description">${mp3 ? 'A smaller file that is easy to share and use in video projects.' : `Uncompressed stereo audio, exported at ${data.bitDepth}-bit.`} Saves to your browser’s download location.</p>`, `<div class="mc-dialog-actions"><button data-action="close">Cancel</button><button data-action="download" class="mc-primary">${icon('download')}Export ${data.format.toUpperCase()}</button></div>`);
  }
  async function download() {
    if (!playable() || state.modalData.exporting) return;
    const data = state.modalData;
    data.filename = $('#export-filename').value.trim().replace(/\.(wav|mp3)$/i, '') || `${state.clip.title}_${state.clip.duration}s`;
    data.exporting = true; dialogError('');
    $('.mc-dialog').querySelectorAll('button,input,select').forEach(element => { element.disabled = true; });
    $('[data-action="download"]').textContent = 'Preparing download…';
    try {
      const parameters = new URLSearchParams({format: data.format, bitrate: String(data.bitrate), rate: String(data.rate), bitDepth: String(data.bitDepth), filename: data.filename});
      const response = await fetch(`/api/clips/${encodeURIComponent(state.clip.id)}/export?${parameters}`);
      if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.error || error.message || 'Export could not be completed.'); }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = `${data.filename}.${data.format}`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      data.exporting = false; closeModal(); toast(`${data.format.toUpperCase()} clip downloaded.`);
    } catch (error) {
      data.exporting = false;
      $('.mc-dialog').querySelectorAll('button,input,select').forEach(element => { element.disabled = false; });
      $('[data-action="download"]').innerHTML = `${icon('download')}Export ${data.format.toUpperCase()}`;
      dialogError(error.message);
    }
  }

  function settingsDialog(tab = 'general') {
    const opening = state.modal !== 'settings';
    if (opening) state.modalData = {defaultDuration: state.settings.defaultDuration, exportFormat: state.settings.exportFormat, bitrate: state.settings.bitrate};
    state.modal = 'settings'; state.modalData.tab = tab;
    renderSettings();
    if (opening) refreshCurrentScan();
  }
  function renderSettings() {
    const data = state.modalData;
    const tabs = `<div class="mc-setting-tabs">${[['general','General'],['folders','Folders'],['export','Export']].map(([tab, name]) => `<button data-action="settings-tab" data-value="${tab}" aria-pressed="${data.tab === tab}">${name}</button>`).join('')}</div>`;
    let body;
    if (data.tab === 'folders') {
      body = `<h3>Library folders</h3><p class="mc-settings-description">Add a parent folder containing your collections. Each collection subfolder appears in the sidebar. Local folders and network shares both work.</p><div id="folder-roots"></div><div id="folder-scan-status" class="mc-scan-status mc-scan-in-dialog" aria-label="Folder scan status"></div><div class="mc-folder-add"><label for="folder-path">Parent folder or network share<input id="folder-path" placeholder="C:\\Music  or  \\\\server\\share\\Music" aria-label="Music parent folder path"></label><button data-action="add-folder" ${state.scanning || state.scanRequestPending ? 'disabled' : ''}>${icon('plus')}${state.scanning ? 'Scan in progress' : 'Add folder & scan collections'}</button></div><p>Remove disconnects a folder from this library. Its music files stay in place.</p>`;
    } else if (data.tab === 'export') {
      body = `<div class="mc-dialog-fields"><label>Default format<select id="default-format" aria-label="Default export format">${formatOptions(data.exportFormat)}</select></label><label>MP3 quality<select id="default-bitrate" aria-label="Default MP3 bitrate">${bitrateOptions(data.bitrate)}</select></label></div><p>Choose the format you use most. You can change it for each export.</p>`;
    } else {
      body = `<div class="mc-dialog-fields"><label>Default clip length<input id="default-duration" value="${editableDuration(data.defaultDuration)}" aria-label="Default clip length" inputmode="numeric" pattern="[0-9:]+" maxlength="9" autocomplete="off" spellcheck="false"></label><div><span class="mc-small mc-muted">Appearance</span><p style="color:var(--mc-text)">Dark mode</p></div></div><p>Use minutes:seconds or hours:minutes:seconds, up to two hours. This default is used when you next open Music Crafter.</p>`;
    }
    modal('Settings', tabs + body, `<div class="mc-dialog-actions"><button data-action="close">Close</button>${data.tab === 'folders' ? `<button data-action="rescan" class="mc-primary" ${state.scanning || state.scanRequestPending ? 'disabled' : ''}>${icon('refresh')}Rescan folders</button>` : '<button data-action="save-settings" class="mc-primary">Save settings</button>'}</div>`);
    renderFolderRoots(); renderScanStatus();
  }
  function captureSettingsDraft() {
    if ($('#default-duration')) state.modalData.defaultDuration = parseDuration($('#default-duration').value);
    if ($('#default-format')) state.modalData.exportFormat = $('#default-format').value;
    if ($('#default-bitrate')) state.modalData.bitrate = Number($('#default-bitrate').value);
  }
  async function saveSettings() {
    captureSettingsDraft();
    const data = state.modalData;
    if (!safeDuration(data.defaultDuration)) { dialogError('Enter a default length between 0:04 and 120:00.'); return; }
    data.saving = true;
    $('[data-action="save-settings"]').disabled = true;
    try {
      const patch = {defaultDuration: data.defaultDuration, exportFormat: data.exportFormat, bitrate: data.bitrate};
      await persistSettings(patch); state.settings = {...state.settings, ...patch};
      data.saving = false; closeModal(); toast('Settings saved.');
    } catch (error) { data.saving = false; $('[data-action="save-settings"]').disabled = false; dialogError(error.message); }
  }
  function scanIsActive(scan = state.scan) { return Boolean(scan && ['queued', 'running'].includes(scan.status)); }
  function scanPercentage(scan = state.scan) {
    return scan && Number(scan.total) > 0 && typeof scan.progress === 'number' && Number.isFinite(scan.progress)
      ? Math.max(0, Math.min(100, Math.round(scan.progress * 100))) : null;
  }
  function scanHeadline() {
    const percentage = scanPercentage();
    return percentage == null ? 'Finding music collections…' : `Scanning collections · ${percentage}%`;
  }
  function scanMarkup(inDialog = false) {
    const scan = state.scan;
    if (!scan && !state.scanRequestPending) return '<p class="mc-small mc-muted">Ready to scan your music folders.</p>';
    const percentage = scanPercentage();
    const active = state.scanning || state.scanRequestPending;
    const warnings = values(scan?.warnings).map(warning => typeof warning === 'string' ? warning : warning.message || warning.error || JSON.stringify(warning));
    const title = active ? state.scanRequestPending ? 'Starting library scan…' : scanHeadline()
      : scan?.status === 'error' ? 'Library scan could not finish' : scan?.status === 'complete' ? 'Library scan complete' : 'Library is ready';
    const message = state.scanConnectionError || scan?.error || scan?.message || (active ? 'Looking for music in your folders.' : 'Choose Rescan to check for new collections.');
    const counters = [];
    if (scan?.foldersScanned != null) counters.push(`${Number(scan.foldersScanned)} folders checked`);
    if (active && Number(scan?.elapsedSeconds) >= 1) counters.push(`${formatTime(scan.elapsedSeconds)} elapsed`);
    if (Number(scan?.total) > 0) counters.push(`${Number(scan.completed) || 0} of ${Number(scan.total)} libraries read`);
    else if (scan?.mapsFound != null) counters.push(`${Number(scan.mapsFound)} library files found`);
    if (!active && scan?.status !== 'idle') counters.push(`${Number(scan?.trackCount) || 0} tracks · ${Number(scan?.collectionCount) || 0} collections`);
    return `<div class="mc-scan-heading"><strong>${esc(title)}</strong>${!inDialog ? '<button class="mc-ghost" data-action="folders">Details</button>' : ''}</div><p class="mc-scan-message ${scan?.status === 'error' || state.scanConnectionError ? 'mc-error' : ''}" role="status" aria-live="polite">${esc(message)}</p>${active ? `<div class="mc-progress ${percentage == null ? 'mc-indeterminate' : ''}" role="progressbar" aria-label="Library scan" ${percentage == null ? '' : `aria-valuenow="${percentage}" aria-valuemin="0" aria-valuemax="100"`}><div${percentage == null ? '' : ` style="width:${percentage}%"`}></div></div>` : ''}${counters.length ? `<p class="mc-scan-counters">${esc(counters.join(' · '))}</p>` : ''}${active && scan?.currentPath ? `<p class="mc-scan-path">Reading: ${esc(scan.currentPath)}</p>` : ''}${active ? '<p class="mc-form-hint">You can keep browsing, previewing, and crafting clips while this runs.</p>' : ''}${warnings.length ? `<details class="mc-scan-warnings" data-scan-warnings ${state.scanWarningsOpen ? 'open' : ''}><summary>${warnings.length} ${warnings.length === 1 ? 'file or folder needs attention' : 'files or folders need attention'}</summary><ul>${warnings.map(warning => `<li>${esc(warning)}</li>`).join('')}</ul></details>` : ''}`;
  }
  function renderFolderRoots() {
    const container = $('#folder-roots');
    if (!container) return;
    const roots = state.library.roots.map(root => typeof root === 'string' ? root : root.path);
    const key = JSON.stringify(roots);
    if (container.dataset.rootsKey !== key) {
      container.dataset.rootsKey = key;
      container.innerHTML = roots.map(path => `<div class="mc-folder"><div class="mc-folder-heading">${icon('folder')}<strong>Music folder</strong><button class="mc-folder-remove" data-action="remove-folder" data-path="${esc(path)}" aria-label="Remove ${esc(path)} from library">Remove</button></div><code>${esc(path)}</code></div>`).join('') || '<p class="mc-small mc-muted">No folders connected yet.</p>';
    }
    container.querySelectorAll('[data-action="remove-folder"]').forEach(button => { button.disabled = state.scanRequestPending; });
    const addButton = $('[data-action="add-folder"]');
    if (addButton) { addButton.disabled = state.scanning || state.scanRequestPending; addButton.innerHTML = `${icon('plus')}${state.scanning ? 'Scan in progress' : 'Add folder & scan collections'}`; }
    const rescanButton = $('.mc-dialog [data-action="rescan"]');
    if (rescanButton) rescanButton.disabled = state.scanning || state.scanRequestPending;
  }
  function renderScanStatus() {
    if ($('#folder-scan-status')) $('#folder-scan-status').innerHTML = scanMarkup(true);
    renderFolderRoots();
  }
  async function reloadCatalogAfterScan(scan, quiet = false) {
    try {
      const library = await api('/api/library');
      if (state.scan?.id !== scan.id) return;
      state.library = normalizeLibrary(library);
      if (!track()) {
        const first = state.library.tracks[0]; state.trackId = first?.id || null; state.tempo = first?.bpm || 0;
        state.variation = 'auto'; state.pitch = 0; state.loop = false;
      }
      if (state.collectionId && !state.library.collections.some(item => String(item.id) === state.collectionId)) state.collectionId = null;
      renderAll(); refreshVariationAvailability(true); renderFolderRoots();
      if (!quiet) toast(`Library updated: ${state.library.collections.length} collections, ${state.library.tracks.length} tracks${values(scan.warnings).length ? '. See scan details for files that need attention.' : '.'}`);
    } catch (error) { state.scanConnectionError = error.message; renderScanStatus(); }
  }
  function applyScan(scan, resume = false, quiet = false) {
    if (!scan) return;
    const previous = state.scan;
    state.scan = {...(previous?.id === scan.id ? previous : {}), ...scan};
    state.scanning = scanIsActive(); state.scanConnectionError = '';
    if (Array.isArray(scan.roots)) state.library.roots = scan.roots;
    renderNavigation(); renderScanStatus();
    if (state.scan.status === 'complete' && state.scan.id !== completedScanId) {
      completedScanId = state.scan.id;
      reloadCatalogAfterScan(state.scan, quiet);
    }
    if (resume && state.scanning) startScanPolling();
  }
  function startScanPolling() {
    clearTimeout(scanTimer);
    const revision = ++scanRevision;
    const poll = async () => {
      try {
        const scan = await api('/api/scan');
        if (revision !== scanRevision) return;
        applyScan(scan);
      } catch (error) {
        if (revision !== scanRevision) return;
        state.scanConnectionError = `Reconnecting to scan progress. ${error.message}`; renderScanStatus();
      }
      if (revision === scanRevision && (state.scanning || state.scanConnectionError)) scanTimer = setTimeout(poll, 500);
    };
    scanTimer = setTimeout(poll, 0);
  }
  async function refreshCurrentScan() {
    try { applyScan(await api('/api/scan'), true, true); }
    catch { /* The folder panel retains the last known scan while a connection returns. */ }
  }
  async function scanFolders(path, remove = false) {
    if (state.scanRequestPending || state.scanning && !remove) return;
    if (!path && !state.library.roots.length) { settingsDialog('folders'); return; }
    const previous = state.scan;
    state.scanRequestPending = true; state.scanning = true; state.scanConnectionError = '';
    clearTimeout(scanTimer); scanRevision++;
    dialogError(''); renderNavigation(); renderScanStatus();
    try {
      const result = await api(remove ? '/api/roots/remove' : path ? '/api/roots' : '/api/rescan', {method: 'POST', body: JSON.stringify(path ? {path} : {})});
      state.scanRequestPending = false;
      applyScan(result, true);
      if (!result.id) await refreshCurrentScan();
    } catch (error) {
      state.scanRequestPending = false; state.scan = previous; state.scanning = scanIsActive();
      state.scanConnectionError = error.message; renderNavigation(); renderScanStatus();
      if (state.modal === 'settings') dialogError(error.message);
      else toast(error.message);
      if (state.scanning) startScanPolling();
    }
  }

  function tempoDialog() {
    const selected = track();
    if (!selected || state.busy) return;
    state.modal = 'tempo'; state.modalData = {tempo: state.tempo, pitch: state.pitch};
    const capability = state.library.capabilities;
    modal('Adjust tempo & pitch', `<p style="margin-bottom:15px">${esc(selected.title)}</p><div class="mc-tempo-reference"><span class="mc-muted">Original tempo</span><strong>${selected.bpm} <span class="mc-small mc-muted">BPM</span></strong></div><div class="mc-tempo-controls"><label>New tempo<input id="tempo-bpm" type="number" step="0.1" min="${(selected.bpm * .8).toFixed(1)}" max="${(selected.bpm * 1.2).toFixed(1)}" value="${state.tempo}" aria-label="New tempo BPM" ${!capability.tempo ? 'disabled' : ''}></label><div><span class="mc-muted mc-small">Speed</span><div class="mc-tempo-ratio"><output id="tempo-percentage">${Math.round(state.tempo / selected.bpm * 1000) / 10}%</output></div></div></div><div class="mc-tempo-slider"><input id="tempo-range" type="range" min="80" max="120" step="0.1" value="${state.tempo / selected.bpm * 100}" aria-label="Tempo speed percentage" ${!capability.tempo ? 'disabled' : ''}><div class="mc-tempo-scale"><span>Slower · 80%</span><span>100%</span><span>Faster · 120%</span></div></div><div class="mc-tempo-presets">${[[90,'90%'],[100,'Original'],[110,'110%']].map(([percentage, name]) => `<button data-action="tempo-preset" data-value="${percentage}" ${!capability.tempo ? 'disabled' : ''}>${name}</button>`).join('')}</div><div class="mc-pitch-row"><label>Pitch change<select id="tempo-pitch" aria-label="Pitch change in semitones" ${!capability.pitch ? 'disabled' : ''}>${Array.from({length:25}, (_, index) => index - 12).map(semitones => `<option value="${semitones}" ${state.pitch === semitones ? 'selected' : ''}>${semitones === 0 ? 'Original pitch' : `${semitones > 0 ? '+' : ''}${semitones} semitone${Math.abs(semitones) === 1 ? '' : 's'}`}</option>`).join('')}</select></label></div><p>Your clip stays ${formatTime(state.duration, true)} long. Apply these settings, then generate to hear the result.</p>`, '<div class="mc-dialog-actions"><button data-action="close">Cancel</button><button data-action="tempo-apply" class="mc-primary">Apply to clip</button></div>');
  }
  function updateTempo(value, updateInput = false) {
    const selected = track();
    const bpm = Math.round(Number(value) * 10) / 10;
    const valid = Number.isFinite(bpm) && bpm >= selected.bpm * .8 - .051 && bpm <= selected.bpm * 1.2 + .051;
    state.modalData.tempo = bpm;
    $('#tempo-percentage').textContent = valid ? `${Math.round(bpm / selected.bpm * 1000) / 10}%` : '—';
    $('[data-action="tempo-apply"]').disabled = !valid;
    dialogError(valid ? '' : `Choose a tempo between ${(selected.bpm * .8).toFixed(1)} and ${(selected.bpm * 1.2).toFixed(1)} BPM.`);
    if (valid) $('#tempo-range').value = bpm / selected.bpm * 100;
    if (updateInput) $('#tempo-bpm').value = bpm;
  }

  document.addEventListener('beforeinput', event => {
    const input = event.target;
    if (!['clip-length','default-duration'].includes(input.id) || !event.data) return;
    const next = input.value.slice(0, input.selectionStart) + event.data + input.value.slice(input.selectionEnd);
    if (!validClockEdit(next)) event.preventDefault();
  });
  document.addEventListener('paste', event => {
    const input = event.target;
    if (!['clip-length','default-duration'].includes(input.id)) return;
    const pasted = event.clipboardData.getData('text').trim();
    const next = input.value.slice(0, input.selectionStart) + pasted + input.value.slice(input.selectionEnd);
    if (!validClockEdit(next)) event.preventDefault();
  });
  document.addEventListener('input', event => {
    const element = event.target;
    if (['clip-length','default-duration'].includes(element.id)) {
      const cleaned = element.value.replace(/[^0-9:]/g, '');
      if (!validClockEdit(cleaned)) element.value = element.dataset.lastClockValue || '';
      else if (cleaned !== element.value) element.value = cleaned;
      element.dataset.lastClockValue = element.value;
    }
    if (element.id === 'track-preview-seek') {
      if (Number.isFinite(previewAudio.duration)) previewAudio.currentTime = previewAudio.duration * Number(element.value) / 1000;
      renderTrackPreview(); return;
    }
    if (element.id === 'track-preview-volume') { previewAudio.volume = Number(element.value)/100; return; }
    if (element.id === 'track-query') { state.query = element.value; renderTrackList(); }
    else if (element.id === 'volume') audio.volume = Number(element.value) / 100;
    else if (element.id === 'track-notes') {
      state.settings.notes[element.dataset.trackId] = element.value;
      clearTimeout(notesTimer); notesTimer = setTimeout(() => { notesTimer = undefined; persistSettings({notes: state.settings.notes}); }, 650);
    } else if (element.id === 'tempo-bpm') updateTempo(element.value);
    else if (element.id === 'tempo-range') updateTempo(track().bpm * Number(element.value) / 100, true);
    else if (element.id === 'clip-length') {
      audio.pause();
      const duration = parseDuration(element.value);
      state.durationError = safeDuration(duration) ? '' : 'Use a valid time from 0:04 to 120:00. Seconds must be below 60.';
      if (!state.durationError) state.duration = duration;
      element.setAttribute('aria-invalid', String(Boolean(state.durationError)));
      $('[data-action="generate"]').disabled = Boolean(state.durationError);
      $('.mc-notice').textContent = state.durationError || 'Generate to hear this new length.';
      $('.mc-notice').classList.toggle('mc-error', Boolean(state.durationError));
      renderPreview();
      refreshVariationAvailability();
    }
  });
  document.addEventListener('change', event => {
    const element = event.target;
    if (element.id === 'mood-filter') { state.mood = element.value; renderTrackList(); }
    else if (element.id === 'energy-filter') { state.energy = element.value; renderTrackList(); }
    else if (element.id === 'sort-tracks') { state.sort = element.value; renderTrackList(); }
    else if (element.id === 'clip-length') {
      const duration = parseDuration(element.value);
      if (safeDuration(duration)) { state.duration = duration; state.durationError = ''; const notice = validateVariation(); setDirty(notice, false); refreshVariationAvailability(true); }
    } else if (element.id === 'clip-loop') { state.loop = element.checked; setDirty(); }
    else if (element.id === 'track-notes') flushNotes();
    else if (element.id === 'tempo-pitch') state.modalData.pitch = Number(element.value);
    else if (element.id === 'export-format') { state.modalData.filename = $('#export-filename').value; state.modalData.format = element.value; renderExport(); }
    else if (element.id === 'export-bitrate') state.modalData.bitrate = Number(element.value);
    else if (element.id === 'export-rate') state.modalData.rate = element.value;
    else if (element.id === 'export-depth') {
      state.modalData.bitDepth = Number(element.value);
      $('#export-description').textContent = `Uncompressed stereo audio, exported at ${state.modalData.bitDepth}-bit. Saves to your browser’s download location.`;
    }
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('button[data-action]');
    if (!button || button.disabled) return;
    const action = button.dataset.action;
    if (action === 'select') selectTrack(button.dataset.id);
    else if (action === 'track-preview') previewTrack(button.dataset.id);
    else if (action === 'track-pause') pauseTrackPreview();
    else if (action === 'track-stop') stopTrackPreview();
    else if (action === 'track-close') closeTrackPreview();
    else if (action === 'track-replay') previewTrack(state.trackPreview.id, true);
    else if (action === 'variation') { if (state.busy) return; state.variation = button.dataset.value; setDirty(); }
    else if (action === 'favorite') {
      const id = String(button.dataset.id);
      state.settings.favorites = favorite(id) ? state.settings.favorites.filter(value => String(value) !== id) : [...state.settings.favorites, id];
      renderNavigation(); renderTrackList(); renderCraft(); persistSettings({favorites: state.settings.favorites});
    } else if (action === 'all' || action === 'favorites' || action === 'clips' || action === 'collection') {
      state.view = action === 'collection' ? 'all' : action;
      state.collectionId = action === 'collection' ? String(button.dataset.id) : null;
      state.query = ''; state.mood = ''; state.energy = '';
      if (action === 'clips') {
        try { const result = await api('/api/clips'); state.clips = result.clips || []; }
        catch (error) { toast(error.message); }
      }
      renderNavigation(); renderFilters(); renderTrackList(); renderClips();
    } else if (action === 'clear-filters') { state.query = ''; state.mood = ''; state.energy = ''; renderFilters(); renderTrackList(); }
    else if (action === 'preset') { if (state.busy) return; state.duration = Number(button.dataset.value); state.durationError = ''; const notice = validateVariation(); setDirty(notice); refreshVariationAvailability(true); }
    else if (action === 'original') {
      state.duration = Math.min(7200, Math.max(4, Math.round(Number(track()?.duration) || 60))); state.durationError = ''; const notice = validateVariation(); setDirty(notice || 'Original recording length selected. Generate to create the arrangement.');
      refreshVariationAvailability(true);
    } else if (action === 'tag') { state.query = button.dataset.value; state.view = 'all'; state.collectionId = null; state.mood = ''; state.energy = ''; renderNavigation(); renderFilters(); renderTrackList(); }
    else if (action === 'tab') { flushNotes(); state.tab = button.dataset.value; renderMetadata(); }
    else if (action === 'generate') generate();
    else if (action === 'play') {
      if (!playable()) return;
      if (!audio.paused) audio.pause();
      else { try { await audio.play(); } catch { toast('The audio could not be played. Try generating the clip again.'); } }
      updateTransport();
    } else if (action === 'stop') { audio.pause(); audio.currentTime = 0; updateTransport(); }
    else if (action === 'open-clip') openClip(button.dataset.id);
    else if (action === 'export') exportDialog();
    else if (action === 'download') download();
    else if (action === 'settings') settingsDialog();
    else if (action === 'folders') settingsDialog('folders');
    else if (action === 'settings-tab') { captureSettingsDraft(); settingsDialog(button.dataset.value); }
    else if (action === 'save-settings') saveSettings();
    else if (action === 'add-folder') { const path = $('#folder-path').value.trim(); if (path) scanFolders(path); else dialogError('Paste the path to your music parent folder.'); }
    else if (action === 'remove-folder') scanFolders(button.dataset.path, true);
    else if (action === 'rescan') scanFolders();
    else if (action === 'tempo') tempoDialog();
    else if (action === 'tempo-preset') updateTempo(track().bpm * Number(button.dataset.value) / 100, true);
    else if (action === 'tempo-apply') {
      const data = {...state.modalData}; closeModal(); state.tempo = data.tempo; state.pitch = data.pitch; const notice = validateVariation(); setDirty(notice || 'Tempo and pitch updated. Generate to hear your clip.');
      refreshVariationAvailability(true);
    } else if (action === 'close') closeModal();
    else if (action === 'retry') load();
  });
  document.addEventListener('keydown', event => {
    const dialog = $('.mc-dialog');
    if (!dialog) return;
    if (event.key === 'Escape') { event.preventDefault(); closeModal(); }
    if (event.key === 'Tab') {
      const focusable = [...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')];
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  $('#waveform').addEventListener('click', event => {
    if (!playable()) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    audio.currentTime = Math.max(0, Math.min(Number(state.clip.duration), (event.clientX - bounds.left) / bounds.width * Number(state.clip.duration)));
    updateTransport();
  });
  $('#waveform').addEventListener('keydown', event => {
    if (!playable()) return;
    if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
      event.preventDefault();
      audio.currentTime = event.key === 'Home' ? 0 : event.key === 'End' ? state.clip.duration : Math.max(0, Math.min(state.clip.duration, audio.currentTime + (event.key === 'ArrowRight' ? 5 : -5)));
      updateTransport();
    }
  });
  for (const name of ['timeupdate','play','pause','ended','loadedmetadata']) audio.addEventListener(name, updateTransport);
  audio.addEventListener('play', () => { previewAudio.pause(); renderTrackPreview(); renderTrackList(); });
  previewAudio.addEventListener('play', () => { audio.pause(); renderTrackPreview(); renderTrackList(); });
  for (const name of ['loadstart','waiting']) previewAudio.addEventListener(name, () => { state.trackPreview.loading = true; renderTrackPreview(); });
  for (const name of ['loadedmetadata','playing','canplay']) previewAudio.addEventListener(name, () => { state.trackPreview.loading = false; state.trackPreview.error = ''; renderTrackPreview(); });
  for (const name of ['pause','ended']) previewAudio.addEventListener(name, () => { renderTrackPreview(); renderTrackList(); });
  for (const name of ['timeupdate','durationchange','volumechange','seeked']) previewAudio.addEventListener(name, renderTrackPreview);
  previewAudio.addEventListener('error', () => {
    if (!state.trackPreview.id) return;
    state.trackPreview.loading = false;
    state.trackPreview.error = 'This recording could not be previewed. Check its music folder, then try replaying.';
    renderTrackPreview(); renderTrackList();
  });
  document.addEventListener('toggle', event => {
    if (event.target.matches?.('[data-scan-warnings]')) state.scanWarningsOpen = event.target.open;
  }, true);
  audio.addEventListener('error', () => { if (state.clip) toast('The saved audio file is unavailable. Generate the clip again.'); });
  const pane = $('.mc-body');
  const resizer = $('#sidebar-resizer');
  let sidebarWidth = 260;
  try { sidebarWidth = Number(localStorage.getItem('music-crafter-sidebar-width')) || 260; } catch { }
  function applySidebarWidth(width, save = false) {
    const maximum = innerWidth <= 700 ? 480 : Math.min(480, Math.max(180, innerWidth - 480));
    sidebarWidth = Math.round(Math.max(180, Math.min(maximum, width)));
    pane.style.setProperty('--mc-sidebar-width', `${sidebarWidth}px`);
    resizer.setAttribute('aria-valuenow', String(sidebarWidth));
    resizer.setAttribute('aria-valuemax', String(maximum));
    resizer.setAttribute('aria-valuetext', `${sidebarWidth} pixels`);
    if (save) { try { localStorage.setItem('music-crafter-sidebar-width', String(sidebarWidth)); } catch { } }
  }
  applySidebarWidth(sidebarWidth);
  let resizeOrigin;
  resizer.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault(); resizer.focus();
    resizeOrigin = {x:event.clientX,width:sidebarWidth};
    resizer.setPointerCapture(event.pointerId);
    document.body.classList.add('mc-resizing');
  });
  resizer.addEventListener('pointermove', event => { if (resizeOrigin) applySidebarWidth(resizeOrigin.width + event.clientX - resizeOrigin.x); });
  function finishResize() { if (!resizeOrigin) return; resizeOrigin = null; document.body.classList.remove('mc-resizing'); applySidebarWidth(sidebarWidth, true); }
  resizer.addEventListener('pointerup', finishResize);
  resizer.addEventListener('lostpointercapture', finishResize);
  resizer.addEventListener('pointercancel', finishResize);
  resizer.addEventListener('dblclick', () => applySidebarWidth(260, true));
  resizer.addEventListener('keydown', event => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault(); applySidebarWidth(event.key === 'Home' ? 180 : event.key === 'End' ? 480 : sidebarWidth + (event.key === 'ArrowRight' ? 16 : -16), true);
  });
  window.addEventListener('resize', () => { if (innerWidth > 700) applySidebarWidth(sidebarWidth); });
  window.addEventListener('beforeunload', () => {
    if (notesTimer) fetch('/api/settings', {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({notes: state.settings.notes}), keepalive: true}).catch(() => {});
  });
  load();
})();
