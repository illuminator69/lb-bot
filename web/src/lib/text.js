// A name put into a sentence (I-029, Z1). The library has Hebrew, Japanese,
// Cyrillic and Latin names side by side, and a right-to-left one reorders the
// neutral characters and the digits beside it: "also להקת הדוגמה · 2 albums"
// was drawn "also 2 · להקת הדוגמה albums".

/** `name`, isolated from the words around it: U+2068 FIRST STRONG ISOLATE …
 *  U+2069 POP DIRECTIONAL ISOLATE — the name keeps its own direction and what
 *  stands beside it keeps its place. Nothing for no name.
 *
 *  For a name JOINED WITH OTHER WORDS in one string or one line. A name that
 *  is an element of its own needs nothing. Never in what is sent to lb-bot, in
 *  a key, or in anything compared or searched: it is two characters more. */
export const iso = name => (name === null || name === undefined || name === '' ? '' : `\u2068${name}\u2069`)
