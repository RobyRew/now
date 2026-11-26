// The admin page's behaviour: pick a view from /admin/api/me, list the nine slots, and send the forms.
// Everything shown is set with textContent; nothing from the API is parsed as HTML.

interface Last {
  title: string;
  artist: string;
  playing: boolean;
  paused: boolean;
  when: string;
  device: string;
}

interface Slot {
  slot: number;
  empty: boolean;
  label?: string;
  username?: string;
  status?: string;
  error?: string;
  since?: string;
  last?: Last | null;
}

interface Now {
  now: {
    playing: boolean;
    paused: boolean;
    title: string;
    artist: string;
    cover: string;
    context: string;
    user: string;
    when: string;
    device: string;
  } | null;
  live: number;
}

const STATUS: Record<string, [string, string]> = {
  live: ['Live', 'rw-badge--ok'],
  idle: ['Idle', ''],
  connecting: ['Connecting', 'rw-badge--accent'],
  reconnecting: ['Reconnecting', 'rw-badge--warn'],
  expired: ['Session expired', 'rw-badge--danger'],
  error: ['Error', 'rw-badge--danger'],
  locked: ['Key changed', 'rw-badge--danger'],
  stopped: ['Stopped', ''],
};

const SIGNIN_PROBLEMS: Record<string, string> = {
  failed: 'Sign-in did not complete. Try again.',
  refused: 'That RobyRew ID account is not allowed here.',
  unavailable: 'The sign-in service did not answer. Try again in a minute.',
};

const REFRESH_MS = 10_000;

function $(selector: string, root: ParentNode = document): HTMLElement {
  const found = root.querySelector<HTMLElement>(selector);
  if (!found) throw new Error(`missing ${selector}`);
  return found;
}

function show(view: string): void {
  for (const section of document.querySelectorAll<HTMLElement>('[data-view]')) {
    section.hidden = section.dataset.view !== view;
  }
}

class SignedOut extends Error {}

async function api<T>(path: string, init: RequestInit = {}): Promise<{ status: number; body: T }> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: { accept: 'application/json', ...(init.body ? { 'content-type': 'application/json' } : {}) },
  });
  if (response.status === 401) throw new SignedOut();
  const text = await response.text();
  return { status: response.status, body: (text ? JSON.parse(text) : {}) as T };
}

function dateLabel(stamp: string): string {
  const moment = new Date(stamp);
  if (Number.isNaN(moment.getTime())) return '';
  return moment.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function lastLine(last: Last | null | undefined): string {
  if (!last) return 'Nothing played since it was added';
  const song = [last.title, last.artist].filter(Boolean).join(' — ');
  if (last.playing) return `Playing: ${song}${last.device ? ` · ${last.device}` : ''}`;
  if (last.paused) return `Paused: ${song}`;
  return `Last: ${song}${last.when ? ` · ${dateLabel(last.when)}` : ''}`;
}

// ── the deck preview ────────────────────────────────────────────────────────

async function renderDeck(): Promise<void> {
  const { body } = await api<Now>('/api/now');
  const now = body.now;
  const cover = $('[data-deck-cover]') as HTMLImageElement;
  $('[data-deck-live]').textContent = `${body.live} account${body.live === 1 ? '' : 's'} connected right now.`;
  if (!now) {
    $('[data-deck-status]').textContent = 'OFF AIR';
    $('[data-deck-title]').textContent = 'Nothing on the deck';
    $('[data-deck-sub]').textContent = '';
    cover.hidden = true;
    return;
  }
  $('[data-deck-status]').textContent = now.playing ? 'NOW PLAYING' : now.paused ? 'PAUSED' : `LAST PLAYED · ${dateLabel(now.when)}`;
  $('[data-deck-title]').textContent = now.title;
  $('[data-deck-sub]').textContent = [now.artist, now.user && `@${now.user}`].filter(Boolean).join(' · ');
  if (now.cover.startsWith('https://i.scdn.co/')) {
    if (cover.dataset.src !== now.cover) {
      cover.dataset.src = now.cover;
      cover.src = now.cover;
    }
    cover.hidden = cover.dataset.broken === now.cover;
  } else {
    cover.hidden = true;
  }
}

// ── the slots ───────────────────────────────────────────────────────────────

const rows = new Map<number, { item: HTMLElement; empty: boolean }>();
// A message to show in a slot's row after the row is rebuilt (an empty slot becomes a filled one on save).
const flash = new Map<number, string>();

function sessionMessage(status: number, body: { error?: string; detail?: string; username?: string }): string {
  if (status === 200) return `Saved. This session belongs to the Spotify account “${body.username ?? ''}”.`;
  switch (body.error) {
    case 'invalid_sp_dc':
      return 'That does not look like an sp_dc value. Copy the cookie’s value only.';
    case 'session_refused':
      return 'Spotify did not accept that session. Log in again in a private window and copy sp_dc again.';
    case 'spotify_unreachable':
      return `Spotify could not be asked right now${body.detail ? ` (${body.detail})` : ''}. Try again in a minute.`;
    default:
      return 'That did not work. Try again.';
  }
}

function buildRow(slot: Slot): HTMLElement {
  const template = document.querySelector<HTMLTemplateElement>('#account-row');
  if (!template) throw new Error('missing #account-row');
  const item = template.content.firstElementChild!.cloneNode(true) as HTMLElement;
  $('[data-slot]', item).textContent = String(slot.slot);
  $('[data-summary]', item).textContent = slot.empty ? 'Add an account' : 'Change';
  ($('[data-rename]', item) as HTMLFormElement).hidden = slot.empty;
  $('[data-remove-wrap]', item).hidden = slot.empty;
  for (const field of item.querySelectorAll<HTMLInputElement>('input[name]')) {
    field.id = `${field.name}-${slot.slot}`;
    const label = field.closest('form')?.querySelector<HTMLLabelElement>('label');
    if (label) label.htmlFor = field.id;
  }

  const sessionForm = $('[data-session]', item) as HTMLFormElement;
  const sessionStatus = $('[data-session-status]', sessionForm);
  const message = flash.get(slot.slot);
  if (message) {
    flash.delete(slot.slot);
    sessionStatus.textContent = message;
    ($('[data-edit]', item) as HTMLDetailsElement).open = true;
  }
  sessionForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const field = sessionForm.elements.namedItem('sp_dc') as HTMLInputElement;
    const button = sessionForm.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    button.disabled = true;
    sessionStatus.classList.remove('rw-hint--error');
    sessionStatus.textContent = 'Checking with Spotify…';
    try {
      const { status, body } = await api<{ error?: string; detail?: string; username?: string }>(
        `/admin/api/accounts/${slot.slot}`,
        { method: 'PUT', body: JSON.stringify({ sp_dc: field.value }) },
      );
      sessionStatus.textContent = sessionMessage(status, body);
      sessionStatus.classList.toggle('rw-hint--error', status !== 200);
      if (status === 200) {
        field.value = '';
        flash.set(slot.slot, sessionStatus.textContent);
        await refresh();
        if (flash.get(slot.slot) === sessionStatus.textContent) flash.delete(slot.slot); // same row, message shown
      }
    } catch (error) {
      if (error instanceof SignedOut) return signedOut();
      sessionStatus.textContent = 'The service did not answer. Try again.';
      sessionStatus.classList.add('rw-hint--error');
    } finally {
      button.disabled = false;
    }
  });

  const renameForm = $('[data-rename]', item) as HTMLFormElement;
  renameForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const field = renameForm.elements.namedItem('label') as HTMLInputElement;
    try {
      await api(`/admin/api/accounts/${slot.slot}`, { method: 'PATCH', body: JSON.stringify({ label: field.value }) });
      await refresh();
    } catch (error) {
      if (error instanceof SignedOut) signedOut();
    }
  });

  $('[data-remove]', item).addEventListener('click', async () => {
    if (!window.confirm(`Remove account ${slot.slot}? Its saved session is deleted.`)) return;
    try {
      await api(`/admin/api/accounts/${slot.slot}`, { method: 'DELETE' });
      await refresh();
    } catch (error) {
      if (error instanceof SignedOut) signedOut();
    }
  });
  return item;
}

function updateRow(item: HTMLElement, slot: Slot): void {
  const status = $('[data-status]', item);
  if (slot.empty) {
    $('[data-name]', item).textContent = 'Empty';
    $('[data-name]', item).classList.add('text-ink-3');
    $('[data-last]', item).textContent = '';
    status.hidden = true;
    return;
  }
  $('[data-name]', item).textContent = slot.label || slot.username || `Account ${slot.slot}`;
  $('[data-name]', item).classList.remove('text-ink-3');
  const who = slot.label && slot.username ? `${slot.username} · ` : '';
  $('[data-last]', item).textContent = who + lastLine(slot.last);
  const [label, tone] = STATUS[slot.status ?? ''] ?? [slot.status ?? '', ''];
  status.hidden = false;
  status.className = `rw-badge shrink-0 ${tone}`.trim();
  status.textContent = label;
  status.title = slot.error ? `${slot.error}, since ${dateLabel(slot.since ?? '')}` : '';
  const labelField = item.querySelector<HTMLInputElement>('input[name="label"]');
  if (labelField && document.activeElement !== labelField) labelField.value = slot.label ?? '';
}

async function renderAccounts(): Promise<void> {
  const { body } = await api<{ accounts: Slot[] }>('/admin/api/accounts');
  const list = $('[data-accounts]');
  for (const slot of body.accounts) {
    let row = rows.get(slot.slot);
    if (!row || row.empty !== slot.empty) {
      const item = buildRow(slot);
      if (row) row.item.replaceWith(item);
      else list.append(item);
      row = { item, empty: slot.empty };
      rows.set(slot.slot, row);
    }
    updateRow(row.item, slot);
  }
}

async function refresh(): Promise<void> {
  await Promise.all([renderAccounts(), renderDeck()]);
}

function signedOut(): void {
  for (const form of document.querySelectorAll<HTMLElement>('[data-signed-in]')) form.hidden = true;
  show('signin');
}

async function start(): Promise<void> {
  const problem = new URLSearchParams(location.search).get('signin');
  const cover = $('[data-deck-cover]') as HTMLImageElement;
  cover.addEventListener('error', () => {
    cover.dataset.broken = cover.dataset.src ?? '';
    cover.hidden = true;
  });
  try {
    const { status, body } = await api<{ name?: string }>('/admin/api/me');
    if (status === 503) return show('off');
    $('[data-me]').textContent = body.name ? `(${body.name})` : '';
    for (const form of document.querySelectorAll<HTMLElement>('[data-signed-in]')) form.hidden = false;
    show('admin');
    await refresh();
    window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh().catch((error) => error instanceof SignedOut && signedOut());
    }, REFRESH_MS);
  } catch (error) {
    if (!(error instanceof SignedOut)) throw error;
    signedOut();
    const message = problem ? SIGNIN_PROBLEMS[problem] : undefined;
    if (message) {
      const line = $('[data-signin-problem]');
      line.textContent = message;
      line.hidden = false;
    }
  }
}

void start();
