window.WBFilterCombobox = (() => {
  const instances = new Map();
  let opened = null;
  function attach(id, values) {
    const input = document.getElementById(id);
    if (!input) return;
    let state = instances.get(id);
    if (!state) {
      const wrap = document.createElement('div');
      wrap.className = 'filter-combobox';
      input.before(wrap); wrap.append(input);
      input.autocomplete = 'off';
      input.setAttribute('role', 'combobox');
      input.setAttribute('aria-autocomplete', 'list');
      input.setAttribute('aria-expanded', 'false');
      input.setAttribute('aria-controls', id + 'Options');
      const toggle = document.createElement('button');
      toggle.type = 'button'; toggle.className = 'filter-combobox-toggle';
      toggle.setAttribute('aria-label', '展开' + input.getAttribute('aria-label') + '选项');
      toggle.innerHTML = '<i class="fa-solid fa-chevron-down" aria-hidden="true"></i>';
      wrap.append(toggle);
      const panel = document.createElement('div');
      panel.id = id + 'Options'; panel.className = 'filter-combobox-panel';
      panel.setAttribute('role', 'listbox');
      panel.setAttribute('aria-label', input.getAttribute('aria-label') + '选项');
      panel.hidden = true; document.body.append(panel);
      state = {input, wrap, panel, values:[], active:-1, matches:[]};
      instances.set(id, state);
      input.addEventListener('focus', () => open(state));
      input.addEventListener('click', () => { if (opened !== state) open(state); });
      input.addEventListener('input', () => open(state));
      input.addEventListener('blur', () => { if (opened === state) close(); });
      toggle.addEventListener('pointerdown', event => event.preventDefault());
      toggle.addEventListener('click', () => {
        if (opened === state) close();
        else { input.focus({preventScroll:true}); open(state); }
      });
      panel.addEventListener('pointerdown', event => event.preventDefault());
      input.addEventListener('keydown', event => {
        if (event.key === 'Escape' && opened === state) { event.preventDefault(); event.stopPropagation(); close(); }
        else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          if (opened !== state) open(state);
          const count = state.matches.length;
          if (count) state.active = state.active < 0 ? (event.key === 'ArrowDown' ? 0 : count - 1) : (state.active + (event.key === 'ArrowDown' ? 1 : -1) + count) % count;
          highlight(state);
        } else if (event.key === 'Enter' && opened === state) {
          event.preventDefault();
          if (state.active >= 0) pick(state, state.matches[state.active]);
          else close();
        } else if (event.key === 'Tab') close();
      });
    }
    state.values = [...new Set(values.filter(value => typeof value === 'string' && value))];
    if (opened === state) render(state);
  }
  function position(state) {
    const r = state.wrap.getBoundingClientRect();
    const below = innerHeight - r.bottom - 12, above = r.top - 12;
    const upward = below < 180 && above > below;
    const height = Math.max(40, Math.min(260, upward ? above : below));
    const width = Math.min(Math.max(r.width, 220), innerWidth - 16);
    Object.assign(state.panel.style, {width:width + 'px', maxHeight:height + 'px',
      left:Math.max(8, Math.min(r.left, innerWidth - width - 8)) + 'px'});
    state.panel.style.top = (upward ? Math.max(8, r.top - state.panel.getBoundingClientRect().height - 4) : r.bottom + 4) + 'px';
  }
  function render(state) {
    const query = state.input.value.trim().toLocaleLowerCase();
    state.matches = ['', ...state.values.filter(value => value.toLocaleLowerCase().includes(query))];
    state.active = -1;
    state.input.removeAttribute('aria-activedescendant');
    state.panel.replaceChildren();
    state.matches.forEach((value, index) => {
      const option = document.createElement('div');
      option.className = 'ui-select-opt'; option.id = state.input.id + 'Option' + index;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', String(state.input.value === value));
      option.textContent = value || state.input.placeholder;
      option.addEventListener('click', () => pick(state, value));
      state.panel.append(option);
    });
    if (query && state.matches.length === 1) {
      const empty = document.createElement('div');
      empty.className = 'filter-combobox-empty'; empty.textContent = '无匹配选项';
      state.panel.append(empty);
    }
    position(state);
  }
  function highlight(state) {
    [...state.panel.querySelectorAll('[role="option"]')].forEach((option, index) => {
      option.classList.toggle('active', index === state.active);
      if (index === state.active) {
        state.input.setAttribute('aria-activedescendant', option.id);
        option.scrollIntoView({block:'nearest'});
      }
    });
  }
  function pick(state, value) {
    state.input.value = value; close();
    state.input.dispatchEvent(new Event('change', {bubbles:true}));
  }
  function open(state) {
    if (opened && opened !== state) close();
    opened = state; state.panel.hidden = false;
    state.input.setAttribute('aria-expanded', 'true'); render(state);
  }
  function close() {
    if (!opened) return;
    opened.panel.hidden = true; opened.input.setAttribute('aria-expanded', 'false');
    opened.input.removeAttribute('aria-activedescendant'); opened = null;
  }
  document.addEventListener('pointerdown', event => {
    if (opened && !opened.wrap.contains(event.target) && !opened.panel.contains(event.target)) close();
  });
  document.addEventListener('scroll', event => {
    if (opened && !opened.panel.contains(event.target)) close();
  }, true);
  window.addEventListener('resize', close);
  return {attach, close};
})();
