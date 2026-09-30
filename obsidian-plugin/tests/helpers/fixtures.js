'use strict';

// Deterministic, fictional vaults for tests. Folder and note names are
// generated; nothing here comes from a real vault.

function mulberry(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function file(path, extension = path.split('.').pop()) {
  const name = path.split('/').pop();
  return { path, name, basename: name.replace(/\.[^.]+$/, ''), extension, stat: { mtime: 1, ctime: 1, size: 10 } };
}

// Builds a TFolder-like tree from a flat list of files.
function folderTree(files) {
  const root = { path: '/', name: '', children: [] };
  const folders = new Map([['', root]]);
  for (const f of files) {
    const parts = f.path.split('/');
    let parent = root, prefix = '';
    for (let i = 0; i < parts.length - 1; i++) {
      prefix = prefix ? prefix + '/' + parts[i] : parts[i];
      let folder = folders.get(prefix);
      if (!folder) { folder = { path: prefix, name: parts[i], children: [] }; folders.set(prefix, folder); parent.children.push(folder); }
      parent = folder;
    }
    parent.children.push(f);
  }
  return root;
}

// A seeded random vault: `notes` Markdown notes spread over generated
// top-level folders, roughly `orphanShare` of them without links, plus a few
// attachments. Returns { files, links, folders }.
function generateVault({ notes = 5000, seed = 7, folders = 6, orphanShare = 0.3, extraLinks = 0.6, attachments = 20 } = {}) {
  const random = mulberry(seed);
  const folderNames = Array.from({ length: folders }, (_, i) => 'area-' + String.fromCharCode(97 + i));
  const files = [];
  for (let i = 0; i < notes; i++) {
    const folder = i % 11 === 0 ? '' : folderNames[Math.floor(random() * folderNames.length)];
    files.push(file((folder ? folder + '/' : '') + 'note-' + i + '.md'));
  }
  for (let i = 0; i < attachments; i++) files.push(file('assets/image-' + i + '.png'));
  const linkedCount = Math.floor(notes * (1 - orphanShare));
  const links = {};
  const add = (a, b) => { if (a === b) return; (links[files[a].path] ||= {})[files[b].path] = 1; };
  // A random tree over the linked notes guarantees each has at least one link,
  // with a few hubs from preferential choices.
  for (let i = 1; i < linkedCount; i++) {
    const parent = random() < 0.35 ? Math.floor(random() * Math.min(i, 12)) : Math.floor(random() * i);
    add(i, parent);
  }
  for (let k = 0; k < Math.floor(linkedCount * extraLinks); k++) add(Math.floor(random() * linkedCount), Math.floor(random() * linkedCount));
  return { files, links, folders: folderNames };
}

module.exports = { mulberry, file, folderTree, generateVault };
