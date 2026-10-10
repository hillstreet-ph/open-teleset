/* CSV stays in the browser; imports never dispatch messages. */
globalThis.OpenTelesetCSV = (() => {
    function parse(text) {
        if (text.length > 2 * 1024 * 1024) throw new Error('CSV must be at most 2 MB.');
        text = text.replace(/^\uFEFF/, '');
        const rows = [];
        let row = [], field = '', quoted = false, closed = false;
        const endField = () => { row.push(field); field = ''; closed = false; };
        const endRow = () => {
            endField();
            if (row.some(value => value.trim())) rows.push(row);
            row = [];
            if (rows.length > 10001) throw new Error('Too many CSV rows.');
        };
        for (let i = 0; i < text.length; i++) {
            const c = text[i];
            if (quoted) {
                if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
                else if (c === '"') { quoted = false; closed = true; }
                else field += c;
            } else if (c === '"') {
                if (field || closed) throw new Error('Unexpected quote in CSV.');
                quoted = true;
            } else if (c === ',') endField();
            else if (c === '\n' || c === '\r') {
                if (c === '\r' && text[i + 1] === '\n') i++;
                endRow();
            } else {
                if (closed) throw new Error('Unexpected text after CSV quote.');
                field += c;
            }
        }
        if (quoted) throw new Error('Unclosed CSV quote.');
        if (field || row.length || closed) endRow();
        return rows;
    }

    function recipients(text, limit) {
        const rows = parse(text);
        if (!rows.length) throw new Error('CSV is empty.');
        const headers = rows.shift().map(value => value.trim().toLowerCase());
        if (new Set(headers).size !== headers.length) throw new Error('Duplicate CSV headers.');
        const recipientIndex = ['recipient', 'username', 'target', 'id'].map(key => headers.indexOf(key)).find(i => i >= 0);
        const messageIndex = headers.indexOf('message');
        if (recipientIndex === undefined) {
            if (messageIndex >= 0 && rows.length === 1 && rows[0].length === headers.length && rows[0][messageIndex].trim()) {
                const message = rows[0][messageIndex];
                if (message.length > 4096) throw new Error('Message exceeds 4096 characters.');
                return { targets: [], messages: {}, message };
            }
            throw new Error('Use a recipient, username, target or id header; optional message column.');
        }
        const targets = [], messages = Object.create(null);
        for (const row of rows) {
            if (row.length !== headers.length) throw new Error('CSV columns do not match the header.');
            // Prefer a username, falling back to id for exported scraper rows.
            const raw = row[recipientIndex] || (headers.includes('id') ? row[headers.indexOf('id')] : '');
            const target = raw.trim().toLowerCase().replace(/^@/, '');
            if (!/^(?:[a-z][a-z0-9_]{3,31}|[1-9][0-9]{0,19})$/.test(target)) throw new Error('Invalid Telegram recipient in CSV.');
            const message = messageIndex >= 0 ? row[messageIndex] : '';
            if (message.length > 4096) throw new Error('Message exceeds 4096 characters.');
            if (targets.includes(target)) {
                if ((messages[target] || '') !== message) throw new Error('Conflicting messages for duplicate recipient.');
                continue;
            }
            targets.push(target);
            if (message.trim()) messages[target] = message;
        }
        if (!targets.length || targets.length > limit) throw new Error(`Import 1–${limit} recipients per run.`);
        return { targets, messages };
    }

    function encode(rows) {
        return '\uFEFF' + rows.map(row => row.map(value => {
            let text = String(value ?? '');
            // Neutralize formulas when opened in spreadsheet applications.
            if (/^[\s]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text;
            return '"' + text.replace(/"/g, '""') + '"';
        }).join(',')).join('\r\n') + '\r\n';
    }
    return { parse, recipients, encode };
})();
