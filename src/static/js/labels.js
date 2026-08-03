// Brother QL Printer App - Label media catalogue and searchable label picker

/**
 * Every label type the backend accepts, with the Brother products you can
 * actually buy for it.
 *
 * The identifiers and the geometry (width, length, printable pixels, form
 * factor) mirror the media table of the printing library, so this list is in
 * lock step with the <select id="label-size"> options in index.html. The
 * product codes, materials and use descriptions come from Brother's regional
 * consumable lists and raster command references.
 *
 * Notes on the data:
 * - Almost every DK roll carries two codes for one physical product: Europe
 *   doubles the leading digits (DK-11201) where the US and Japan do not
 *   (DK-1201). Both are listed so a search finds the roll either way.
 * - Die-cut identifiers are always media width x label length. Brother's shops
 *   quote the short side first ("29 mm x 62 mm" for 62x29), which is why the
 *   search also matches the reversed size.
 * - Entries without a product code are deliberately left empty rather than
 *   guessed; their notes explain why.
 *
 * @type {Array<{identifier: string, product_codes: string[], name: string,
 *   description: string, form: string, width_mm: number, length_mm: ?number,
 *   printable_px: {width: number, length: number}, material: string,
 *   notes: string, confidence: string}>}
 */
const LABEL_CATALOGUE = [
    {
        identifier: "12",
        product_codes: ["DK-22214", "DK-2214"],
        name: "12 mm continuous",
        description: "White continuous-length paper tape. Narrow general-purpose labelling: cable and wire marking, shelf edges, spine and small-item labels.",
        form: "continuous",
        width_mm: 12,
        length_mm: null,
        printable_px: { width: 106, length: 0 },
        material: "paper",
        notes: "Roll length 30.48 m. DK-22214 is the EU code, DK-2214 the US/JP code for the same roll. Fits every QL model.",
        confidence: "confirmed"
    },
    {
        identifier: "12+17",
        product_codes: [],
        name: "12 mm continuous, full-width raster",
        description: "",
        form: "continuous",
        width_mm: 12,
        length_mm: null,
        printable_px: { width: 306, length: 0 },
        material: "paper",
        notes: "Not a Brother medium and not present in Brother's raster command reference. Compatibility entry added by the brother_ql-inventree fork (fork PR #42, from upstream pklaus/brother_ql PR #95): 12 mm continuous media addressed with the full 29 mm-wide (306 px) raster, i.e. 12 mm of printable label plus roughly 17 mm of liner. The roll physically loaded is still an ordinary 12 mm continuous roll (DK-22214 / DK-2214). Prefer the '12' identifier; only reach for this one if a printer rejects '12' with a media mismatch.",
        confidence: "unverified"
    },
    {
        identifier: "18",
        product_codes: [],
        name: "18 mm continuous",
        description: "",
        form: "continuous",
        width_mm: 18,
        length_mm: null,
        printable_px: { width: 234, length: 0 },
        material: "tape",
        notes: "No Brother DK roll of this width exists and 18 mm does not appear in any QL raster command reference. The geometry (234 printable dots for 18 mm = 360 dpi, feed margin 14) matches an 18 mm P-touch TZe cassette on the 360 dpi PT-P900W / PT-P950NW head, not a QL medium. Could not confirm against a Brother source; no product code asserted. Unlikely to be usable on a QL printer.",
        confidence: "unverified"
    },
    {
        identifier: "29",
        product_codes: ["DK-22210", "DK-2210", "DK-22211", "DK-2211"],
        name: "29 mm continuous",
        description: "White continuous-length tape. Address, filing and general-purpose labels at a length you choose.",
        form: "continuous",
        width_mm: 29,
        length_mm: null,
        printable_px: { width: 306, length: 0 },
        material: "paper (DK-22210 / DK-2210, 30.48 m); film (DK-22211 / DK-2211, 15.24 m)",
        notes: "Two distinct products share this identifier: the paper roll and the more durable film roll. DK-222xx are EU codes, DK-22xx US/JP codes for the same rolls. Fits every QL model.",
        confidence: "confirmed"
    },
    {
        identifier: "38",
        product_codes: ["DK-22225", "DK-2225"],
        name: "38 mm continuous",
        description: "White continuous-length paper tape. Medium-width general labelling.",
        form: "continuous",
        width_mm: 38,
        length_mm: null,
        printable_px: { width: 413, length: 0 },
        material: "paper",
        notes: "Roll length 30.48 m. Fits every QL model.",
        confidence: "confirmed"
    },
    {
        identifier: "50",
        product_codes: ["DK-22223", "DK-2223"],
        name: "50 mm continuous",
        description: "White continuous-length paper tape. Wide general labelling.",
        form: "continuous",
        width_mm: 50,
        length_mm: null,
        printable_px: { width: 554, length: 0 },
        material: "paper",
        notes: "Roll length 30.48 m. Fits every QL model.",
        confidence: "confirmed"
    },
    {
        identifier: "54",
        product_codes: ["DK-N55224", "DK-N5224"],
        name: "54 mm continuous (non-adhesive)",
        description: "White continuous-length non-adhesive paper tape. Tickets, tags, badges and receipts that must not stick to anything.",
        form: "continuous",
        width_mm: 54,
        length_mm: null,
        printable_px: { width: 590, length: 0 },
        material: "non-adhesive paper",
        notes: "Roll length 30.48 m. The only non-adhesive DK medium. DK-N55224 is the EU code, DK-N5224 the US code. Media is 53.8 mm wide even though it is sold as 54 mm.",
        confidence: "confirmed"
    },
    {
        identifier: "62",
        product_codes: ["DK-22205", "DK-2205", "DK-22212", "DK-2212", "DK-22113", "DK-2113", "DK-22606", "DK-2606", "DK-44205", "DK-4205", "DK-44605", "DK-4605"],
        name: "62 mm continuous",
        description: "Continuous-length tape, the widest that fits every QL model. Shipping, signage, barcode and general labels at a length you choose.",
        form: "continuous",
        width_mm: 62,
        length_mm: null,
        printable_px: { width: 696, length: 0 },
        material: "paper (DK-22205); film (DK-22212 white, DK-22113 clear, DK-22606 yellow); removable adhesive paper (DK-44205 white, DK-44605 yellow)",
        notes: "Six distinct products share this identifier, differing only in material and colour: DK-22205 white paper 30.48 m, DK-22212 white film 15.24 m, DK-22113 clear film 15.24 m, DK-22606 yellow film 15.24 m, DK-44205 white removable paper 30.48 m, DK-44605 yellow removable paper 30.48 m. DK-2xxxx are the EU codes, DK-2xxx / DK-4xxx the US/JP codes. Fits every QL model.",
        confidence: "confirmed"
    },
    {
        identifier: "62red",
        product_codes: ["DK-22251", "DK-2251"],
        name: "62 mm continuous, black/red",
        description: "Continuous-length paper tape that prints black and red. Warnings, priority markings, price and promotion labels.",
        form: "continuous",
        width_mm: 62,
        length_mm: null,
        printable_px: { width: 696, length: 0 },
        material: "paper (two-colour thermal)",
        notes: "Roll length 15.24 m. Two-colour media: only the QL-800, QL-810W and QL-820NWB can print the red channel. Loading it in any other QL prints black only. Same geometry as '62' - the difference is purely the colour capability.",
        confidence: "confirmed"
    },
    {
        identifier: "102",
        product_codes: ["DK-22243", "DK-2243"],
        name: "102 mm continuous",
        description: "White continuous-length paper tape. Wide shipping and pallet labels at a length you choose.",
        form: "continuous",
        width_mm: 102,
        length_mm: null,
        printable_px: { width: 1164, length: 0 },
        material: "paper",
        notes: "Roll length 30.48 m. Wide-format media: only QL-1050, QL-1060N, QL-1100, QL-1110NWB and QL-1115NWB accept it. Actual media width is 101.6 mm.",
        confidence: "confirmed"
    },
    {
        identifier: "103",
        product_codes: ["DK-22246", "DK-2246"],
        name: "103 mm continuous",
        description: "White continuous-length paper tape. The widest continuous medium; 4x6-class shipping labels at a length you choose.",
        form: "continuous",
        width_mm: 104,
        length_mm: null,
        printable_px: { width: 1200, length: 0 },
        material: "paper",
        notes: "Roll length 30.48 m. Sold as 103 mm; actual media width is 103.6 mm and the printer reports it as 104 mm, which is why the library records width 104. brother_ql restricts this identifier to QL-1100 and QL-1110NWB. Brother lists the roll as compatible with QL-1050/1060N/1115NWB as well - on those models use the '104' identifier instead.",
        confidence: "confirmed"
    },
    {
        identifier: "104",
        product_codes: ["DK-22246", "DK-2246"],
        name: "104 mm continuous (legacy)",
        description: "Same physical roll as '103'; legacy identifier kept for the older wide-format QL models.",
        form: "continuous",
        width_mm: 104,
        length_mm: null,
        printable_px: { width: 1200, length: 0 },
        material: "paper",
        notes: "Not a separate product. Same 103.6 mm DK-22246 / DK-2246 roll as '103', but with a different right offset (-8 instead of 12) and a wider model allowance: QL-1050, QL-1060N, QL-1100, QL-1110NWB, QL-1115NWB. If output is shifted sideways on a QL-1100/1110NWB, switch to '103'.",
        confidence: "likely"
    },
    {
        identifier: "17x54",
        product_codes: ["DK-11204", "DK-1204"],
        name: "17 x 54 mm die-cut",
        description: "Multi purpose labels. Brother's general-purpose small die-cut label.",
        form: "die-cut",
        width_mm: 17,
        length_mm: 54,
        printable_px: { width: 165, length: 566 },
        material: "paper",
        notes: "400 labels per roll. Fits every QL model. DK-11204 is the EU code, DK-1204 the US/JP code.",
        confidence: "confirmed"
    },
    {
        identifier: "17x87",
        product_codes: ["DK-11203", "DK-1203"],
        name: "17 x 87 mm die-cut",
        description: "File folder labels.",
        form: "die-cut",
        width_mm: 17,
        length_mm: 87,
        printable_px: { width: 165, length: 956 },
        material: "paper",
        notes: "300 labels per roll. Fits every QL model. DK-11203 is the EU code, DK-1203 the US/JP code.",
        confidence: "confirmed"
    },
    {
        identifier: "23x23",
        product_codes: ["DK-11221", "DK-1221"],
        name: "23 x 23 mm square die-cut",
        description: "Square paper labels. Small square labels for coding, marking and (in Japan) small food declarations.",
        form: "die-cut",
        width_mm: 23,
        length_mm: 23,
        printable_px: { width: 202, length: 202 },
        material: "paper",
        notes: "1000 labels per roll. Fits every QL model. Too small for most barcodes. DK-11221 is the EU code, DK-1221 the US/JP code.",
        confidence: "confirmed"
    },
    {
        identifier: "29x42",
        product_codes: ["DK-11215", "DK-1215"],
        name: "29 x 42 mm die-cut",
        description: "Food declaration and specimen labels.",
        form: "die-cut",
        width_mm: 29,
        length_mm: 42,
        printable_px: { width: 306, length: 425 },
        material: "paper (thermal, fluorescent-brightener free)",
        notes: "700 labels per roll. Regional: sold by Brother Japan as DK-1215 and available in Europe as DK-11215; it does not appear on Brother's US or UK per-printer consumable lists, so US buyers may not find it. The media size itself (29 mm x 42 mm, ID 358) is in Brother's QL raster command reference, so every QL accepts it.",
        confidence: "likely"
    },
    {
        identifier: "29x90",
        product_codes: ["DK-11201", "DK-1201"],
        name: "29 x 90 mm die-cut",
        description: "Standard address labels. Brother's most common die-cut roll.",
        form: "die-cut",
        width_mm: 29,
        length_mm: 90,
        printable_px: { width: 306, length: 991 },
        material: "paper",
        notes: "400 labels per roll. Fits every QL model. DK-11201 is the EU code, DK-1201 the US/JP code.",
        confidence: "confirmed"
    },
    {
        identifier: "39x90",
        product_codes: ["DK-11208", "DK-1208"],
        name: "38 x 90 mm die-cut",
        description: "Large address labels.",
        form: "die-cut",
        width_mm: 38,
        length_mm: 90,
        printable_px: { width: 413, length: 991 },
        material: "paper",
        notes: "400 labels per roll. The identifier says 39 but the medium is 38 mm wide - Brother and the library both record 38.0 mm. Fits every QL model. DK-11208 is the EU code, DK-1208 the US/JP code.",
        confidence: "confirmed"
    },
    {
        identifier: "39x48",
        product_codes: ["DK-11220", "DK-1220"],
        name: "39 x 48 mm die-cut",
        description: "Food declaration labels. Also used for return addresses, barcodes and FNSKU labels.",
        form: "die-cut",
        width_mm: 39,
        length_mm: 48,
        printable_px: { width: 425, length: 495 },
        material: "paper",
        notes: "700 labels per roll. Regional: Brother Japan sells it as DK-1220 (food declaration label), Europe as DK-11220. The media size (39 mm x 48 mm, ID 367) is in Brother's QL raster command reference, so every QL accepts it.",
        confidence: "confirmed"
    },
    {
        identifier: "52x29",
        product_codes: ["DK-11226", "DK-1226", "DK-A226"],
        name: "52 x 29 mm die-cut",
        description: "Food declaration and specimen labels.",
        form: "die-cut",
        width_mm: 52,
        length_mm: 29,
        printable_px: { width: 578, length: 271 },
        material: "paper (thermal); DK-A226 is an alcohol-resistant variant",
        notes: "1000 labels per roll. Regional: Brother Japan sells DK-1226, Europe DK-11226. DK-A226 is a Japan-only alcohol-resistant version of the same size aimed at medical specimen tubes. Media size 52 mm x 29 mm (ID 374) is in Brother's raster reference, so every QL accepts it.",
        confidence: "confirmed"
    },
    {
        identifier: "54x29",
        product_codes: ["DK-3235"],
        name: "54 x 29 mm die-cut (removable)",
        description: "Small removable labels. Food safety dating, packages and envelopes - anywhere the label has to come off cleanly.",
        form: "die-cut",
        width_mm: 54,
        length_mm: 29,
        printable_px: { width: 598, length: 271 },
        material: "paper with removable adhesive",
        notes: "800 labels per roll. Brother markets it as '29 mm x 54 mm' (short side first); the media is 54 mm wide and 29 mm long, matching raster media ID 382. Brother lists compatibility only for QL-800, QL-810W and QL-820NWB. Minor geometry discrepancy: Brother's raster reference gives 638 total / 602 printable dots, brother_ql uses 630 / 598, so expect the print to sit about 0.3 mm narrower than the sheet.",
        confidence: "likely"
    },
    {
        identifier: "60x86",
        product_codes: ["DK-11234", "DK-1234"],
        name: "60 x 86 mm die-cut",
        description: "Name badge / visitor badge labels.",
        form: "die-cut",
        width_mm: 60,
        length_mm: 87,
        printable_px: { width: 672, length: 954 },
        material: "paper (adhesive)",
        notes: "260 labels per roll. Brother quotes the length as 86 mm; the medium is 86.8 mm and the library records 87. DK-11234 is the EU code, DK-1234 the US code. Fits every QL model.",
        confidence: "confirmed"
    },
    {
        identifier: "62x29",
        product_codes: ["DK-11209", "DK-1209"],
        name: "62 x 29 mm die-cut",
        description: "Small address labels. Also widely used for barcodes, asset tags and shelf labels.",
        form: "die-cut",
        width_mm: 62,
        length_mm: 29,
        printable_px: { width: 696, length: 271 },
        material: "paper",
        notes: "800 labels per roll. Brother's shops list this as '29 mm x 62 mm' (short side first) which reads like a 29 mm roll - it is not. Brother's own raster command reference records media ID 274 as 62.0 mm wide x 28.9 mm long, matching this identifier. There is no 29x62 identifier. Fits every QL model.",
        confidence: "confirmed"
    },
    {
        identifier: "62x100",
        product_codes: ["DK-11202", "DK-1202"],
        name: "62 x 100 mm die-cut",
        description: "Shipping labels for parcels and packages.",
        form: "die-cut",
        width_mm: 62,
        length_mm: 100,
        printable_px: { width: 696, length: 1109 },
        material: "paper",
        notes: "300 labels per roll. Fits every QL model. DK-11202 is the EU code, DK-1202 the US/JP code.",
        confidence: "confirmed"
    },
    {
        identifier: "102x51",
        product_codes: ["DK-11240", "DK-1240"],
        name: "102 x 51 mm die-cut",
        description: "Barcode labels. Brother's large multi-purpose / barcode die-cut.",
        form: "die-cut",
        width_mm: 102,
        length_mm: 51,
        printable_px: { width: 1164, length: 526 },
        material: "paper",
        notes: "600 labels per roll. Wide-format media: only QL-1050, QL-1060N, QL-1100, QL-1110NWB and QL-1115NWB accept it. DK-11240 is the EU code, DK-1240 the US code.",
        confidence: "confirmed"
    },
    {
        identifier: "102x152",
        product_codes: ["DK-11241", "DK-1241"],
        name: "102 x 152 mm die-cut",
        description: "Large shipping labels - the 4 x 6 inch carrier label format.",
        form: "die-cut",
        width_mm: 102,
        length_mm: 153,
        printable_px: { width: 1164, length: 1660 },
        material: "paper",
        notes: "200 labels per roll. Wide-format media: only QL-1050, QL-1060N, QL-1100, QL-1110NWB and QL-1115NWB accept it. Actual sheet is 101.6 x 152.8 mm, which is why the library records length 153. DK-11241 is the EU code, DK-1241 the US code.",
        confidence: "confirmed"
    },
    {
        identifier: "103x164",
        product_codes: ["DK-11247", "DK-1247"],
        name: "103 x 164 mm die-cut",
        description: "Large shipping labels. The biggest die-cut Brother makes for the QL range.",
        form: "die-cut",
        width_mm: 104,
        length_mm: 164,
        printable_px: { width: 1200, length: 1822 },
        material: "paper",
        notes: "180 labels per roll. Sold as 103 mm; actual sheet is 103.6 x 164.3 mm and the printer reports the media as 104 mm wide, which is why the library records width 104. Restricted to QL-1100 and QL-1110NWB. DK-11247 is the EU code, DK-1247 the US code.",
        confidence: "confirmed"
    },
    {
        identifier: "d12",
        product_codes: ["DK-11219", "DK-1219"],
        name: "12 mm round",
        description: "Round paper labels. Small dot labels for coding, sealing and pricing.",
        form: "round-die-cut",
        width_mm: 12,
        length_mm: 12,
        printable_px: { width: 94, length: 94 },
        material: "paper",
        notes: "1200 labels per roll. 12 mm diameter; only 8 mm of it is printable, so keep content tiny. Fits every QL model. DK-11219 is the EU code, DK-1219 the US/JP code.",
        confidence: "confirmed"
    },
    {
        identifier: "d24",
        product_codes: ["DK-11218", "DK-1218"],
        name: "24 mm round",
        description: "Round paper labels. General-purpose dot labels; the usual choice for small QR codes.",
        form: "round-die-cut",
        width_mm: 24,
        length_mm: 24,
        printable_px: { width: 236, length: 236 },
        material: "paper",
        notes: "1000 labels per roll. 24 mm diameter, 20 mm printable. Fits every QL model. DK-11218 is the EU code, DK-1218 the US/JP code.",
        confidence: "confirmed"
    },
    {
        identifier: "d58",
        product_codes: ["DK-11207", "DK-1207"],
        name: "58 mm round",
        description: "CD/DVD labels.",
        form: "round-die-cut",
        width_mm: 58,
        length_mm: 58,
        printable_px: { width: 618, length: 618 },
        material: "film",
        notes: "100 labels per roll. The only round DK made of film rather than paper. Fits every QL model. DK-11207 is the EU code, DK-1207 the US/JP code.",
        confidence: "confirmed"
    },];

/**
 * Display order and headings of the picker's groups, keyed by the catalogue's
 * "form" value. Groups are listed in this order; entries inside a group are
 * sorted by ascending width.
 */
const LABEL_GROUPS = [
    { form: 'continuous', title: 'Continuous rolls' },
    { form: 'die-cut', title: 'Die-cut labels' },
    { form: 'round', title: 'Round labels' }
];

/**
 * Words a user is likely to type for a form factor but that do not appear in
 * the catalogue text itself (the UI says "continuous", the roll box and the
 * old dropdown said "endless").
 */
const LABEL_FORM_KEYWORDS = {
    'continuous': 'continuous endless roll tape any length',
    'die-cut': 'die-cut diecut precut fixed size sheet',
    'round-die-cut': 'round circle circular dot die-cut diecut'
};

/**
 * Map a catalogue "form" value to the group it is shown in. Round media is a
 * die-cut too, but it gets its own group because users look for it by shape.
 * @param {string} form - catalogue form value
 * @returns {string} group key used by LABEL_GROUPS
 */
function labelGroupKey(form) {
    if (form === 'round-die-cut') return 'round';
    return form;
}

/**
 * Normalise a string for searching: lower case, with everything that is not a
 * letter or a digit removed. "DK-11218", "dk 11218" and "dk11218" all collapse
 * to the same token, so a product code matches however it is typed.
 * @param {*} value - any value; null/undefined become an empty string
 * @returns {string} the normalised token
 */
function normaliseLabelTerm(value) {
    return String(value == null ? '' : value).toLowerCase().replace(/[^a-z0-9]+/g, '');
}

/**
 * Build the list of searchable strings for one catalogue entry. Each field is
 * matched on its own so a query can never match across two unrelated fields.
 * @param {Object} entry - a LABEL_CATALOGUE entry
 * @returns {string[]} normalised, non-empty search fields
 */
function labelSearchFields(entry) {
    const fields = [
        entry.identifier,
        entry.name,
        entry.description,
        entry.material,
        entry.notes,
        entry.form,
        LABEL_FORM_KEYWORDS[entry.form] || ''
    ];

    // Product codes, exactly as printed on the box.
    entry.product_codes.forEach(code => fields.push(code));

    // The physical size, in both orders: Brother advertises die-cut labels
    // short-side-first ("29 mm x 62 mm"), the identifier is width first.
    if (entry.length_mm === null) {
        fields.push(entry.width_mm + ' mm');
    } else {
        fields.push(entry.width_mm + 'x' + entry.length_mm);
        fields.push(entry.length_mm + 'x' + entry.width_mm);
        fields.push(entry.width_mm + ' mm x ' + entry.length_mm + ' mm');
        fields.push(entry.length_mm + ' mm x ' + entry.width_mm + ' mm');
    }

    return fields.map(normaliseLabelTerm).filter(field => field.length > 0);
}

/**
 * The catalogue with its search fields pre-computed, built once on load.
 */
const LABEL_SEARCH_INDEX = LABEL_CATALOGUE.map(entry => ({
    entry: entry,
    fields: labelSearchFields(entry)
}));

/**
 * Does the query, taken as one phrase, occur in any field?
 *
 * Both sides are normalised, so punctuation and spacing simply drop out:
 * "DK-11218", "dk 11218" and "11218" all find the same roll, and "29 x 62"
 * finds 62x29 through its reversed-size field.
 *
 * @param {string[]} fields - normalised search fields of one entry
 * @param {string} compact - the normalised query
 * @returns {boolean}
 */
function labelEntryMatchesPhrase(fields, compact) {
    return fields.some(field => field.indexOf(compact) !== -1);
}

/**
 * Does every word of the query occur in some field (not necessarily the same
 * one)? This is the loose fallback that catches queries whose words are spread
 * over several fields, e.g. "round 24" - shape in one field, size in another.
 *
 * @param {string[]} fields - normalised search fields of one entry
 * @param {string[]} words - the normalised query words
 * @returns {boolean}
 */
function labelEntryMatchesWords(fields, words) {
    return words.every(word => fields.some(field => field.indexOf(word) !== -1));
}

/**
 * All catalogue entries matching a query, in catalogue order.
 *
 * The search runs in two stages so the precise rule always wins: entries are
 * matched against the whole query first, and only when that finds nothing does
 * the per-word fallback run. Without that order, "24 mm" would drag in every
 * entry that mentions "mm" somewhere alongside a stray "24".
 *
 * A query with no letters or digits at all (whitespace, punctuation) counts as
 * no query and returns the full catalogue.
 *
 * @param {string} query - the raw query as typed
 * @returns {Object[]} matching catalogue entries
 */
function filterLabelEntries(query) {
    const compact = normaliseLabelTerm(query);
    if (!compact) return LABEL_CATALOGUE.slice();

    const phraseHits = LABEL_SEARCH_INDEX
        .filter(item => labelEntryMatchesPhrase(item.fields, compact))
        .map(item => item.entry);
    if (phraseHits.length > 0) return phraseHits;

    const words = String(query).trim().split(/\s+/)
        .map(normaliseLabelTerm)
        .filter(word => word.length > 0);
    if (words.length < 2) return [];

    return LABEL_SEARCH_INDEX
        .filter(item => labelEntryMatchesWords(item.fields, words))
        .map(item => item.entry);
}

/**
 * The picker's reading order for two entries: ascending width, then length,
 * then identifier. Shared by every group, so a roll sits in the same relative
 * place wherever it is shown.
 * @param {Object} a - a catalogue entry
 * @param {Object} b - a catalogue entry
 * @returns {number} the usual comparator sign
 */
function compareLabelEntries(a, b) {
    return a.width_mm - b.width_mm ||
        (a.length_mm || 0) - (b.length_mm || 0) ||
        a.identifier.localeCompare(b.identifier);
}

/**
 * Split entries into the picker's groups, in group order, each sorted by
 * ascending width (then length, then identifier). Empty groups are dropped.
 * @param {Object[]} entries - catalogue entries
 * @returns {Array<{title: string, entries: Object[]}>}
 */
function groupLabelEntries(entries) {
    const groups = [];

    LABEL_GROUPS.forEach(group => {
        const members = entries
            .filter(entry => labelGroupKey(entry.form) === group.form)
            .sort(compareLabelEntries);
        if (members.length > 0) {
            groups.push({ title: group.title, entries: members });
        }
    });

    return groups;
}

/**
 * The catalogue entry for a label type identifier.
 * @param {string} identifier - e.g. "d24"
 * @returns {?Object} the entry, or null when the identifier is unknown
 */
function labelEntryFor(identifier) {
    for (let i = 0; i < LABEL_CATALOGUE.length; i++) {
        if (LABEL_CATALOGUE[i].identifier === identifier) return LABEL_CATALOGUE[i];
    }
    return null;
}

/**
 * The product codes of an entry as one line, e.g. "DK-11218 / DK-1218".
 * @param {Object} entry - a catalogue entry
 * @returns {string} the codes, or an empty string when the entry has none
 */
function labelCodesText(entry) {
    return entry.product_codes.join(' / ');
}

/**
 * The product codes of an entry, abbreviated for the closed picker: the leading
 * code, and how many others share this identifier.
 *
 * "62 mm continuous" alone covers twelve products, and a line of twelve codes
 * is what turned the closed control into a paragraph. The leading code is the
 * one worth reordering by (and the one the copy button puts on the clipboard);
 * the count says the list is longer without spelling it out. The full list is
 * one click away in the open list, and in the trigger's tooltip.
 * @param {?Object} entry - a catalogue entry
 * @returns {string} e.g. "DK-11218", "DK-22205 +11", or "" without any code
 */
function labelCodeSummary(entry) {
    const code = labelPrimaryCode(entry);
    if (!code) return '';
    const others = entry.product_codes.length - 1;
    return others > 0 ? code + ' +' + others : code;
}

/**
 * The closed picker's tooltip: everything the one line cannot hold, so nothing
 * that used to be on display is actually gone.
 * @param {?Object} entry - a catalogue entry
 * @returns {string} name, description and every product code, one per line
 */
function labelTriggerTitle(entry) {
    if (!entry) return '';
    const parts = [entry.name];
    if (entry.description) parts.push(entry.description);
    const codes = labelCodesText(entry);
    if (codes) parts.push(codes);
    return parts.join('\n');
}

/**
 * The code to put on the clipboard for an entry, or an empty string when the
 * medium has no Brother product behind it.
 *
 * Only the first code, never the joined list: this is meant to be pasted into
 * a shop's search box, and "DK-11218 / DK-1218" finds nothing. The regional
 * pair is listed in full on the entry itself, and the button names the code it
 * will copy, so there is no guessing about which one lands on the clipboard.
 * @param {Object} entry - a catalogue entry
 * @returns {string}
 */
function labelPrimaryCode(entry) {
    return (entry && entry.product_codes.length > 0) ? entry.product_codes[0] : '';
}

/**
 * Put text on the clipboard, resolving to whether it worked.
 *
 * navigator.clipboard only exists in a secure context, and this app is
 * routinely reached over plain http on a LAN address, where it is absent. The
 * hidden-textarea fallback is what actually runs in that (common) case.
 * @param {string} text - the text to copy
 * @returns {Promise<boolean>} true when the copy succeeded
 */
function copyTextToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text).then(() => true, () => false);
    }
    return Promise.resolve(legacyCopyToClipboard(text));
}

/**
 * Clipboard fallback for non-secure contexts: select a detached textarea and
 * let the browser's own copy command take it.
 * @param {string} text - the text to copy
 * @returns {boolean} true when the copy succeeded
 */
function legacyCopyToClipboard(text) {
    const area = document.createElement('textarea');
    area.value = text;
    // Keep it out of sight and out of the layout, but still selectable -
    // display:none or visibility:hidden would make the selection fail.
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    area.style.opacity = '0';
    document.body.appendChild(area);

    let copied = false;
    try {
        area.select();
        copied = document.execCommand('copy');
    } catch (error) {
        copied = false;
    }
    document.body.removeChild(area);
    return copied;
}

/**
 * A caveat worth showing next to an entry, or an empty string when there is
 * none. Two identifiers have no Brother product behind them at all.
 * @param {Object} entry - a catalogue entry
 * @returns {{text: string, kind: string}|null}
 */
function labelFlag(entry) {
    if (entry.product_codes.length === 0) {
        return { text: 'No Brother product code — unverified', kind: 'muted' };
    }
    return null;
}

// ===================== Searchable label picker =====================
//
// The native <select id="label-size"> stays in the DOM and remains the single
// source of truth: the picker only writes to it and then fires a "change"
// event, so every other module keeps reading the value it always read. The
// select is hidden from view (and from the tab order) once the picker is live,
// which also means the plain dropdown still works if this script fails to run.

// Identifier the keyboard cursor currently sits on while the picker is open.
let labelPickerActive = null;

/**
 * The places the one popover can be anchored, as host id -> its trigger and
 * what a choice made there means.
 *
 * There is deliberately only one list in the page. The Settings field is its
 * home; the top bar's medium pill borrows it while it is open, so changing the
 * roll from the header and changing it in Settings are the same control with
 * the same contents, and cannot drift apart. The owned-media field borrows it
 * too, for the same reason in reverse: thirty identifiers with their product
 * codes and their grouping already exist here, and a second list of them would
 * be a second place to keep correct.
 *
 * Two modes:
 *   'select' - one choice, written to #label-size, and the list closes;
 *   'own'    - a membership list, toggled row by row, and the list stays open.
 */
const LABEL_PICKER_HOSTS = {
    'label-picker': { trigger: 'label-picker-trigger', mode: 'select' },
    'medium-switcher': { trigger: 'navbar-medium', mode: 'select' },
    'owned-media': { trigger: 'owned-media-add', mode: 'own' }
};

/** The host the popover lives in while closed. */
const LABEL_PICKER_HOME = 'label-picker';

// Host id the popover is currently anchored to, or null while it is closed.
let labelPickerHostId = null;

// What a choice means right now: 'select' or 'own'. Always back to 'select'
// when the list is closed, so nothing can be left in the other mode.
let labelPickerMode = 'select';

/**
 * What the printer reports as loaded, in the form the picker needs: the
 * candidate identifiers and, when there is more than one, why.
 *
 * media.js pushes this in; labels.js never fetches anything itself, so the
 * picker keeps working (and stays testable) on its own.
 * @type {{candidates: string[], reason: string}}
 */
let labelPickerLoadedMedia = { candidates: [], reason: '' };

/**
 * Tell the picker which label types the printer reports as loaded, so they can
 * be offered first. Passing nothing clears the detection and returns the list
 * to exactly what it looked like before this feature existed.
 * @param {?{candidates: string[], reason: string}} info - detected media
 */
function setLabelPickerLoadedMedia(info) {
    const candidates = (info && Array.isArray(info.candidates))
        ? info.candidates.filter(id => typeof id === 'string' && id.length > 0)
        : [];
    const reason = (info && typeof info.reason === 'string') ? info.reason : '';
    labelPickerLoadedMedia = { candidates: candidates, reason: reason };

    // Repaint an open list, so a roll changed while the user is looking at the
    // list moves to the top under them rather than after the next open.
    if (labelPickerHostId) {
        const search = document.getElementById('label-picker-search');
        renderLabelPickerList(search ? search.value : '');
    }
}

/**
 * The detected candidates that the <select> actually accepts, as catalogue
 * entries. Anything unknown is dropped rather than offered.
 * @returns {Object[]} catalogue entries, in the order the printer reported them
 */
function loadedLabelEntries() {
    const select = document.getElementById('label-size');
    if (!select) return [];

    const available = {};
    Array.prototype.forEach.call(select.options, option => { available[option.value] = true; });

    return labelPickerLoadedMedia.candidates
        .filter(identifier => available[identifier])
        .map(labelEntryFor)
        .filter(entry => entry !== null);
}

/**
 * The entries to show under "My media": the ones the user says they own, minus
 * anything already shown as loaded.
 *
 * It is fed the query's matches rather than the whole catalogue, so the group
 * narrows with the search like every other group does - searching for a roll
 * you own finds it under "My media" instead of hiding it, and searching for one
 * you do not own simply leaves the group out. Owning nothing leaves it out too,
 * which is the default and wants no heading and no prompt.
 *
 * Ownership is read through media.js's ownsMedium() behind a guard, the same
 * way buildLabelOption() reads it, so labels.js still stands up on its own.
 *
 * @param {Object[]} entries - the catalogue entries matching the query
 * @param {Object} isLoaded - identifier -> true for the rows already shown as
 *   loaded, which are not repeated here
 * @returns {Object[]} owned entries in the picker's usual reading order
 */
function ownedLabelEntries(entries, isLoaded) {
    if (typeof ownsMedium !== 'function') return [];
    return entries
        .filter(entry => !isLoaded[entry.identifier] && ownsMedium(entry.identifier))
        .sort(compareLabelEntries);
}

/**
 * Wire up the label picker. Called from setupEventListeners() in core.js.
 */
function setupLabelPicker() {
    const select = document.getElementById('label-size');
    const picker = document.getElementById('label-picker');
    const trigger = document.getElementById('label-picker-trigger');
    const search = document.getElementById('label-picker-search');
    const list = document.getElementById('label-picker-list');
    if (!select || !picker || !trigger || !search || !list) return;

    // Warn (once, in the console) if the dropdown and the catalogue drift
    // apart - an option without an entry cannot be offered by the picker.
    const known = {};
    LABEL_CATALOGUE.forEach(entry => { known[entry.identifier] = true; });
    const orphans = Array.prototype.filter.call(select.options, option => !known[option.value]);
    if (orphans.length > 0) {
        console.warn('Label types missing from the catalogue:',
            orphans.map(option => option.value).join(', '));
    }

    // Take over from the native control.
    picker.hidden = false;
    select.classList.add('lp-native');
    select.setAttribute('tabindex', '-1');
    select.setAttribute('aria-hidden', 'true');
    const labelEl = document.getElementById('label-size-label');
    if (labelEl) labelEl.removeAttribute('for');

    const copyButton = document.getElementById('label-picker-copy');
    if (copyButton) copyButton.addEventListener('click', copySelectedLabelCode);

    trigger.addEventListener('click', () => toggleLabelPicker(LABEL_PICKER_HOME));
    trigger.addEventListener('keydown', event => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            openLabelPicker(LABEL_PICKER_HOME);
        }
    });

    search.addEventListener('input', () => renderLabelPickerList(search.value));
    search.addEventListener('keydown', handleLabelPickerKeydown);

    list.addEventListener('click', event => {
        const option = event.target.closest('.lp-opt');
        if (option) commitLabelPickerChoice(option.dataset.identifier);
    });
    list.addEventListener('pointerover', event => {
        const option = event.target.closest('.lp-opt');
        if (option && option.dataset.identifier !== labelPickerActive) {
            setLabelPickerActive(option.dataset.identifier);
        }
    });

    // Clicking or tapping anywhere else closes the picker, wherever it is
    // currently anchored.
    document.addEventListener('pointerdown', event => {
        if (!labelPickerHostId) return;
        const host = document.getElementById(labelPickerHostId);
        if (host && !host.contains(event.target)) closeLabelPicker(false);
    });

    // Any change of the select - by the picker or by code that loads settings -
    // refreshes what the closed picker shows.
    select.addEventListener('change', syncLabelPicker);
    syncLabelPicker();
}

/**
 * Refresh the closed picker from the current value of #label-size. Safe to
 * call at any time; a no-op when the picker is not in the page.
 */
function syncLabelPicker() {
    const select = document.getElementById('label-size');
    const picker = document.getElementById('label-picker');
    if (!select || !picker || picker.hidden) return;

    const nameEl = document.getElementById('label-picker-name');
    const metaEl = document.getElementById('label-picker-meta');
    const triggerEl = document.getElementById('label-picker-trigger');
    const noteEl = document.getElementById('label-picker-note');
    const entry = labelEntryFor(select.value);

    if (nameEl) {
        nameEl.textContent = entry
            ? entry.name
            : (select.value || 'Select a label type');
    }
    if (metaEl) {
        // An entry without codes says so through its caveat line below, so the
        // code slot simply disappears instead of repeating the message.
        const summary = labelCodeSummary(entry);
        metaEl.textContent = summary;
        metaEl.hidden = !summary;
    }
    if (triggerEl) {
        // The closed control is one line, so the description and the full code
        // list are carried by the tooltip - and by the open list, where they
        // have the room to be read.
        const title = labelTriggerTitle(entry);
        if (title) {
            triggerEl.setAttribute('title', title);
        } else {
            triggerEl.removeAttribute('title');
        }
    }
    if (noteEl) {
        const flag = entry ? labelFlag(entry) : null;
        noteEl.textContent = flag ? flag.text : '';
        noteEl.classList.toggle('d-none', !flag);
    }
    syncLabelCopyButton(entry);
}

/**
 * Point the copy button at the selected medium's product code, or hide it when
 * the medium has none. Naming the code on the button is the whole trick: the
 * user sees exactly what they are about to paste.
 * @param {Object|null} entry - the selected catalogue entry, if any
 */
function syncLabelCopyButton(entry) {
    const button = document.getElementById('label-picker-copy');
    const textEl = document.getElementById('label-picker-copy-text');
    if (!button || !textEl) return;

    const code = labelPrimaryCode(entry);
    button.hidden = !code;
    if (!code) {
        // Drop the previous medium's code rather than leaving it parked on a
        // hidden button: a stale product code is how someone ends up ordering
        // the wrong roll.
        resetLabelCopyButton();
        delete button.dataset.code;
        button.removeAttribute('title');
        textEl.textContent = 'Copy code';
        return;
    }

    // A pending "Copied" confirmation belongs to the previous medium.
    resetLabelCopyButton();
    button.dataset.code = code;
    textEl.textContent = 'Copy ' + code;
    button.setAttribute('title', 'Copy ' + code + ' to the clipboard');
}

// Handle for the "Copied" confirmation, so switching media mid-flash does not
// leave the button stuck on the wrong label.
let labelCopyResetTimer = null;

/**
 * Restore the copy button to its resting state.
 */
function resetLabelCopyButton() {
    const button = document.getElementById('label-picker-copy');
    const textEl = document.getElementById('label-picker-copy-text');
    const iconEl = document.getElementById('label-picker-copy-icon');
    if (labelCopyResetTimer) {
        clearTimeout(labelCopyResetTimer);
        labelCopyResetTimer = null;
    }
    if (!button || !textEl || !iconEl) return;
    button.classList.remove('is-copied');
    iconEl.className = 'bi bi-clipboard';
    if (button.dataset.code) textEl.textContent = 'Copy ' + button.dataset.code;
}

/**
 * Copy the selected medium's product code and confirm it on the button itself.
 */
function copySelectedLabelCode() {
    const button = document.getElementById('label-picker-copy');
    const textEl = document.getElementById('label-picker-copy-text');
    const iconEl = document.getElementById('label-picker-copy-icon');
    if (!button || !textEl || !iconEl) return;

    const code = button.dataset.code || '';
    if (!code) return;

    copyTextToClipboard(code).then(copied => {
        if (labelCopyResetTimer) clearTimeout(labelCopyResetTimer);
        button.classList.toggle('is-copied', copied);
        iconEl.className = copied ? 'bi bi-check2' : 'bi bi-exclamation-triangle';
        textEl.textContent = copied ? 'Copied ' + code : 'Press Ctrl+C to copy';
        labelCopyResetTimer = setTimeout(resetLabelCopyButton, 2000);
    });
}

/**
 * Open the picker under one of its hosts: move the popover there, reset the
 * query and focus the search box.
 *
 * Opening it under a host while it is already open under another one closes it
 * first, so there is never more than one popover in the page.
 *
 * @param {string} [hostId] - a key of LABEL_PICKER_HOSTS; defaults to the
 *   Settings field
 */
function openLabelPicker(hostId) {
    const wanted = LABEL_PICKER_HOSTS[hostId] ? hostId : LABEL_PICKER_HOME;
    if (labelPickerHostId === wanted) return;

    const host = document.getElementById(wanted);
    const trigger = document.getElementById(LABEL_PICKER_HOSTS[wanted].trigger);
    const pop = document.getElementById('label-picker-pop');
    const search = document.getElementById('label-picker-search');
    const list = document.getElementById('label-picker-list');
    if (!host || !trigger || !pop || !search) return;

    if (labelPickerHostId) closeLabelPicker(false);

    if (pop.parentNode !== host) host.appendChild(pop);
    // The header anchors the popover to the whole status cluster's right edge;
    // the Settings and owned-media fields stretch it across the field.
    pop.classList.toggle('lp-pop--header', wanted === 'medium-switcher');

    labelPickerHostId = wanted;
    labelPickerMode = LABEL_PICKER_HOSTS[wanted].mode;
    host.classList.add('is-open');
    pop.hidden = false;
    pop.classList.toggle('lp-pop--own', labelPickerMode === 'own');
    trigger.setAttribute('aria-expanded', 'true');
    search.setAttribute('aria-expanded', 'true');
    search.setAttribute('placeholder', labelPickerMode === 'own'
        ? 'Find a roll to tick — DK-11218, 62x29, round…'
        : 'Search DK-11218, 62x29, round, shipping…');
    if (list) {
        // Ticking several rolls off a list is a multi-select; saying so is what
        // stops a screen reader announcing each tick as "the label type is now
        // 62 mm", which is exactly what it is not.
        list.setAttribute('aria-multiselectable', labelPickerMode === 'own' ? 'true' : 'false');
    }
    search.value = '';
    renderLabelPickerList('');
    search.focus();
}

/**
 * Close the picker and park the popover back on the Settings field, so the DOM
 * ends up in the same shape whichever host it was opened from.
 * @param {boolean} focusTrigger - move focus back to the control it opened from
 */
function closeLabelPicker(focusTrigger) {
    const pop = document.getElementById('label-picker-pop');
    if (!pop || !labelPickerHostId) return;

    const host = document.getElementById(labelPickerHostId);
    const trigger = document.getElementById(LABEL_PICKER_HOSTS[labelPickerHostId].trigger);
    const search = document.getElementById('label-picker-search');

    pop.hidden = true;
    pop.classList.remove('lp-pop--header', 'lp-pop--own');
    if (host) host.classList.remove('is-open');
    if (trigger) {
        trigger.setAttribute('aria-expanded', 'false');
        if (focusTrigger) trigger.focus();
    }
    if (search) {
        search.setAttribute('aria-expanded', 'false');
        search.setAttribute('aria-activedescendant', '');
    }

    const home = document.getElementById(LABEL_PICKER_HOME);
    if (home && pop.parentNode !== home) home.appendChild(pop);

    labelPickerHostId = null;
    labelPickerMode = 'select';
    labelPickerActive = null;

    // An automatic media switch holds off while a list is open rather than
    // moving the selection under whoever is reading it; this is where it gets
    // its turn. A no-op when there is nothing to switch to.
    if (typeof mediaPickerClosed === 'function') mediaPickerClosed();
}

/**
 * Is the picker currently open under a particular host?
 * @param {string} hostId - a key of LABEL_PICKER_HOSTS
 * @returns {boolean}
 */
function labelPickerIsOpenIn(hostId) {
    return labelPickerHostId === hostId;
}

/**
 * Is the picker open anywhere? Read by the automatic media switch, which will
 * not move the selection out from under a list somebody is reading.
 * @returns {boolean}
 */
function labelPickerIsOpen() {
    return labelPickerHostId !== null;
}

/**
 * Toggle the picker open/closed under a host (the triggers' click handler).
 * @param {string} [hostId] - a key of LABEL_PICKER_HOSTS
 */
function toggleLabelPicker(hostId) {
    const wanted = LABEL_PICKER_HOSTS[hostId] ? hostId : LABEL_PICKER_HOME;
    if (labelPickerHostId === wanted) {
        closeLabelPicker(true);
    } else {
        openLabelPicker(wanted);
    }
}

/**
 * The DOM id of an option row. Identifiers contain characters that are awkward
 * in an id ("12+17", "62x29"), so they are reduced to letters and digits.
 * @param {string} identifier - label type identifier
 * @returns {string} the element id
 */
function labelOptionId(identifier) {
    return 'lp-opt-' + normaliseLabelTerm(identifier);
}

/**
 * Build one option row.
 * @param {Object} entry - a catalogue entry
 * @param {string} current - the currently selected identifier
 * @param {string} [loaded] - '' for an ordinary row, 'only' when the printer
 *   identified this medium outright, 'one-of' when it is one of several the
 *   printer cannot tell apart
 * @returns {HTMLLIElement}
 */
function buildLabelOption(entry, current, loaded) {
    // In "own" mode a row is a tick box rather than a choice: what it reports
    // as selected is membership of the owned list, not what the app prints for.
    const owning = labelPickerMode === 'own';
    const owned = owning && typeof ownsMedium === 'function' && ownsMedium(entry.identifier);

    const option = document.createElement('li');
    option.className = 'lp-opt' + (loaded ? ' lp-opt--loaded' : '') + (owned ? ' lp-opt--owned' : '');
    option.id = labelOptionId(entry.identifier);
    option.dataset.identifier = entry.identifier;
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected',
        (owning ? owned : entry.identifier === current) ? 'true' : 'false');

    const head = document.createElement('span');
    head.className = 'lp-opt-head';

    if (owning) {
        const tick = document.createElement('i');
        tick.className = owned ? 'bi bi-check-square-fill lp-tick' : 'bi bi-square lp-tick';
        tick.setAttribute('aria-hidden', 'true');
        head.appendChild(tick);

        // Room for a preferred-variant control, when there is one to build.
        // Ticking both 62 and 62red is the case ownership cannot decide, and
        // the conflict is visible here, on the tick that caused it - so this is
        // where the choice belongs, appended to `head` after the tick. The row
        // is in that state exactly when
        //
        //   (MEDIA_VARIANT_GROUPS[mediaMemoryKeyFor(entry.identifier)] || [])
        //       .filter(ownsMedium).length > 1
        //
        // using only what media.js already exports. It also needs a stored
        // preference of its own and a place for it in the resolution order,
        // neither of which is invented here.
    }

    const name = document.createElement('span');
    name.className = 'lp-opt-name';
    name.textContent = entry.name;
    head.appendChild(name);

    if (loaded) {
        const mark = document.createElement('span');
        mark.className = 'lp-loaded-mark';
        mark.textContent = loaded === 'one-of' ? 'Loaded — one of these' : 'Loaded';
        head.appendChild(mark);
    }

    // One or two codes are the regional pair of a single product and trail the
    // name. More than that means several products share the identifier (62 mm
    // alone covers six) - those get a line of their own so none is cut off.
    // Entries without a code show their caveat flag instead.
    const codesText = labelCodesText(entry);
    const manyCodes = entry.product_codes.length > 2;
    let codes = null;
    if (codesText) {
        codes = document.createElement('span');
        codes.className = manyCodes ? 'lp-opt-codes lp-opt-codes--block' : 'lp-opt-codes';
        codes.textContent = codesText;
        if (!manyCodes) head.appendChild(codes);
    }

    option.appendChild(head);

    if (entry.description) {
        const description = document.createElement('span');
        description.className = 'lp-opt-desc';
        description.textContent = entry.description;
        option.appendChild(description);
    }

    if (codes && manyCodes) option.appendChild(codes);

    const flag = labelFlag(entry);
    if (flag) {
        const flagEl = document.createElement('span');
        flagEl.className = 'lp-flag lp-flag--' + flag.kind;
        flagEl.textContent = flag.text;
        option.appendChild(flagEl);
    }

    return option;
}

/**
 * Render the option list for a query, grouped and sorted, and place the
 * keyboard cursor on the selected entry (or on the first result).
 * @param {string} query - the raw query as typed
 */
function renderLabelPickerList(query) {
    const select = document.getElementById('label-size');
    const list = document.getElementById('label-picker-list');
    const empty = document.getElementById('label-picker-empty');
    if (!select || !list) return;

    // Only offer what the <select> actually accepts.
    const available = {};
    Array.prototype.forEach.call(select.options, option => { available[option.value] = true; });
    const matches = filterLabelEntries(query).filter(entry => available[entry.identifier]);
    const current = select.value;

    // Two shortcuts sit above the catalogue, in this order:
    //
    //   1. what the printer says is in it. At most two rows, it is the answer
    //      to "which one should I pick right now" almost every time the list is
    //      opened, and when it is ambiguous it carries the explanation and the
    //      "pick one" heading. It keeps the top spot: a personal shortlist of
    //      unbounded length above it could push the actionable row out of view,
    //      and that is the one moment the picker exists for.
    //   2. "My media" - what the user says they own. Most people have three or
    //      four rolls, and finding them should not mean reading thirty rows.
    //
    // Both are taken out of the ordinary groups rather than repeated in them:
    // one row per identifier keeps the ids unique and the arrow keys sane. A
    // roll that is both loaded and owned appears once, under "Loaded in the
    // printer", so the two groups never claim the same row. Everything else
    // stays exactly where it was, because preparing a job for a roll that is
    // not loaded - or not owned yet - is a perfectly ordinary thing to do.
    //
    // Neither in "own" mode: that list is about the cupboard, not about the
    // printer, promoting the loaded roll in it would suggest that ticking a row
    // says something about what is in the machine right now, and hoisting the
    // ticked rows would make every tick shuffle the list under the cursor -
    // which is precisely the case that list is built to make quick.
    const owning = labelPickerMode === 'own';
    const loadedEntries = owning ? [] : loadedLabelEntries();
    const isLoaded = {};
    loadedEntries.forEach(entry => { isLoaded[entry.identifier] = true; });

    const loadedMatches = matches.filter(entry => isLoaded[entry.identifier]);
    const mineMatches = owning ? [] : ownedLabelEntries(matches, isLoaded);
    const isMine = {};
    mineMatches.forEach(entry => { isMine[entry.identifier] = true; });
    const groups = groupLabelEntries(matches.filter(entry =>
        !isLoaded[entry.identifier] && !isMine[entry.identifier]));

    list.innerHTML = '';
    let firstIdentifier = null;
    let currentVisible = false;

    if (owning) {
        const note = document.createElement('li');
        note.className = 'lp-groupnote lp-groupnote--lead';
        note.setAttribute('role', 'presentation');
        note.textContent = 'Tick the rolls you actually have. They get a "My media" group at ' +
            'the top of the label picker, so the few you use are the first thing you see ' +
            'instead of all thirty.';
        list.appendChild(note);
    }

    if (loadedMatches.length > 0) {
        const head = document.createElement('li');
        head.className = 'lp-grouphead lp-grouphead--loaded';
        head.setAttribute('role', 'presentation');
        head.textContent = loadedMatches.length > 1
            ? 'Loaded in the printer — pick one'
            : 'Loaded in the printer';
        list.appendChild(head);

        // Several candidates means the printer genuinely cannot tell them
        // apart, and which one it is is the user's intent rather than a fact
        // the device can supply. Say why, and choose nothing.
        if (loadedEntries.length > 1 && labelPickerLoadedMedia.reason) {
            const note = document.createElement('li');
            note.className = 'lp-groupnote';
            note.setAttribute('role', 'presentation');
            note.textContent = labelPickerLoadedMedia.reason;
            list.appendChild(note);
        }

        const mark = loadedEntries.length > 1 ? 'one-of' : 'only';
        loadedMatches.forEach(entry => {
            list.appendChild(buildLabelOption(entry, current, mark));
            if (firstIdentifier === null) firstIdentifier = entry.identifier;
            if (entry.identifier === current) currentVisible = true;
        });
    }

    // The user's own rolls. No heading when there are none to put under it, so
    // the default - owning nothing - looks exactly like the list always did.
    if (mineMatches.length > 0) {
        const head = document.createElement('li');
        head.className = 'lp-grouphead lp-grouphead--mine';
        head.setAttribute('role', 'presentation');
        head.textContent = 'My media';
        list.appendChild(head);

        mineMatches.forEach(entry => {
            list.appendChild(buildLabelOption(entry, current, ''));
            if (firstIdentifier === null) firstIdentifier = entry.identifier;
            if (entry.identifier === current) currentVisible = true;
        });
    }

    groups.forEach(group => {
        const head = document.createElement('li');
        head.className = 'lp-grouphead';
        head.setAttribute('role', 'presentation');
        head.textContent = group.title;
        list.appendChild(head);

        group.entries.forEach(entry => {
            list.appendChild(buildLabelOption(entry, current, ''));
            if (firstIdentifier === null) firstIdentifier = entry.identifier;
            if (entry.identifier === current) currentVisible = true;
        });
    });

    if (empty) {
        const noMatch = matches.length === 0;
        empty.hidden = !noMatch;
        if (noMatch) {
            empty.textContent = 'No label matches "' + String(query).trim() + '". ' +
                'Try a product code (DK-11218), a size (62x29) or a use (shipping).';
        }
    }

    setLabelPickerActive(currentVisible ? current : firstIdentifier);
}

/**
 * Move the keyboard cursor to an option and tell assistive technology about it.
 * @param {?string} identifier - the option to activate, or null for none
 */
function setLabelPickerActive(identifier) {
    const list = document.getElementById('label-picker-list');
    const search = document.getElementById('label-picker-search');
    if (!list) return;

    labelPickerActive = identifier || null;
    let activeEl = null;

    list.querySelectorAll('.lp-opt').forEach(option => {
        const isActive = option.dataset.identifier === labelPickerActive;
        option.classList.toggle('is-active', isActive);
        if (isActive) activeEl = option;
    });

    if (search) search.setAttribute('aria-activedescendant', activeEl ? activeEl.id : '');
    if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
}

/**
 * Move the keyboard cursor by one step (or to either end of the list).
 * @param {string} key - the pressed key: ArrowDown / ArrowUp / Home / End
 */
function moveLabelPickerActive(key) {
    const list = document.getElementById('label-picker-list');
    if (!list) return;

    const options = Array.prototype.slice.call(list.querySelectorAll('.lp-opt'));
    if (options.length === 0) return;

    const index = options.findIndex(option => option.dataset.identifier === labelPickerActive);
    let next;

    if (key === 'Home') {
        next = 0;
    } else if (key === 'End') {
        next = options.length - 1;
    } else if (key === 'ArrowDown') {
        next = index < 0 ? 0 : (index + 1) % options.length;
    } else {
        next = index < 0 ? options.length - 1 : (index - 1 + options.length) % options.length;
    }

    setLabelPickerActive(options[next].dataset.identifier);
}

/**
 * Keyboard handling for the search box: filter while typing, arrows to move,
 * Enter to select, Escape to close. Enter is always swallowed so it cannot
 * submit the settings form the picker sits in.
 * @param {KeyboardEvent} event
 */
function handleLabelPickerKeydown(event) {
    const key = event.key;

    if (key === 'Escape') {
        event.preventDefault();
        closeLabelPicker(true);
    } else if (key === 'Enter') {
        event.preventDefault();
        if (labelPickerActive) commitLabelPickerChoice(labelPickerActive);
    } else if (key === 'Tab') {
        closeLabelPicker(false);
    } else if (key === 'ArrowDown' || key === 'ArrowUp' || key === 'Home' || key === 'End') {
        event.preventDefault();
        moveLabelPickerActive(key);
    }
}

/**
 * Act on a row, according to what the list is currently for.
 *
 * The two meanings are kept apart here rather than inside
 * selectLabelIdentifier(), which is called from outside this module (the
 * mismatch buttons, the automatic switch) and must always mean exactly one
 * thing: set the label type.
 *
 * @param {string} identifier - label type identifier
 */
function commitLabelPickerChoice(identifier) {
    if (!identifier) return;

    if (labelPickerMode === 'own') {
        if (typeof toggleOwnedMedium === 'function') toggleOwnedMedium(identifier);
        // Owning several rolls is the normal case, so the list stays open and
        // the query stays put: ticking three boxes should not cost three trips
        // through the trigger.
        const search = document.getElementById('label-picker-search');
        renderLabelPickerList(search ? search.value : '');
        setLabelPickerActive(identifier);
        return;
    }

    selectLabelIdentifier(identifier);
}

/**
 * Commit a choice: write it to #label-size and fire a "change" event so the
 * rest of the app (orientation control, preview, print requests) reacts
 * exactly as it does for the native dropdown.
 * @param {string} identifier - label type identifier, e.g. "d24"
 */
function selectLabelIdentifier(identifier) {
    const select = document.getElementById('label-size');
    if (!select || !identifier) return;

    const known = Array.prototype.some.call(select.options, option => option.value === identifier);
    if (!known) return;

    if (select.value !== identifier) {
        select.value = identifier;
        select.dispatchEvent(new Event('change', { bubbles: true }));
    }

    syncLabelPicker();
    closeLabelPicker(true);
}
