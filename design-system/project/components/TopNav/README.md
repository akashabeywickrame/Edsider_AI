The white header bar across every review app: wordmark left, page links as pills, the signed-in person right (.edsider-navbar).

**Consumer provides:** `pages` (labels), `active`, optional `title` (defaults to "EdSider"), `who`.

- The active page is `primary` on `primary-soft`, bold. Others are `text-dark`, turning `primary` on hover.
- The data-entry tool lists its seven pages; the teacher and assignment apps list one - they are separate apps, not hidden pages.
- Below 900px, collapse the links into a menu button.
- In a real app, put the EdSider AI logo (`assets/Logos/edsider-ai-logo.png`, 50px tall) where the text wordmark sits, linked to the app's first page, followed by a small `primary-soft` pill naming the app ("Question review", "Teacher review", "Paper assignment"). The component's `title` text is the fallback when the image cannot load.
