/* Shared, presentation-only interactions. Publishing stays in the existing forms. */
document.addEventListener('DOMContentLoaded', () => {
    const showThumbnailFallback = image => {
        const thumbnail = image.closest('[data-studio-thumbnail]');
        if (!thumbnail) return;
        image.hidden = true;
        thumbnail.classList.add('is-unavailable');
        const fallback = thumbnail.querySelector('[data-studio-thumbnail-fallback]');
        if (fallback) fallback.hidden = false;
    };
    document.querySelectorAll('[data-studio-thumbnail-image]').forEach(image => {
        image.addEventListener('error', () => showThumbnailFallback(image), {once: true});
        if (image.complete && image.naturalWidth === 0) showThumbnailFallback(image);
    });

    const video = document.querySelector('[data-studio-video]');
    document.querySelectorAll('[data-preview-src]').forEach(button => {
        button.addEventListener('click', () => {
            if (!video) return;
            video.pause();
            video.src = button.dataset.previewSrc;
            video.load();
            document.querySelectorAll('[data-preview-src]').forEach(tab => {
                tab.classList.toggle('active', tab === button);
                tab.setAttribute('aria-pressed', String(tab === button));
            });
        });
    });
    document.querySelectorAll('[data-open-editor]').forEach(link => link.addEventListener('click', () => {
        const editor = document.getElementById('studioEditor');
        if (editor) editor.open = true;
    }));
    document.querySelectorAll('[data-play-preview]').forEach(button => button.addEventListener('click', async () => {
        if (!video) return;
        try { await video.play(); video.focus(); }
        catch (_) { document.getElementById('studioMediaNotice').hidden = false; }
    }));
    if (video) video.addEventListener('error', () => {
        const notice = document.getElementById('studioMediaNotice');
        if (notice) notice.hidden = false;
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') document.body.classList.remove('sidebar-open');
    });
    document.querySelectorAll('.app-nav-link.active').forEach(link => link.setAttribute('aria-current', 'page'));
    // Contextual settings links use the same tab controller as the in-page buttons.
    const section = new URLSearchParams(location.search).get('section');
    const sections = {voice: '#vtab-subtitle-voice', backup: '#vtab-accounts', notifications: '#vtab-notifications'};
    if (sections[section]) document.querySelector(`[data-settings-target="${sections[section]}"]`)?.click();
});
