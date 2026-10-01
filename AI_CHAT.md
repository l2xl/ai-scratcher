# AI chat in the item panel

The chat is a part of the item's content panel rather than a window opened next to it. The item's
description is the first message of the exchange: it is shown once, as the content itself, since it
already closes the seed context, and the replies follow it inside the panel (`.block-chat` under
the description, the controls on the panel's bottom-left border, `.block-ai`).

## Starting a chat

- **By editing.** Editing the description starts a chat: the modified text is its first message.
  While the editor is open the AI controls appear on the bottom border as in a running chat —
  connector · model · effort (`.chat-setup`), the mode switches (`.chat-mode`) and `➤ send`
  (`.chat-send`) — so the setup can be chosen before the text is saved or sent. `➤ send` and
  Ctrl+Enter in the editor save the item and send the saved statement as the user's text of the
  first turn.
- **On a selected item.** With the item merely selected, the `✦ AI chat` tool button (`.chat-open`)
  shows the reply field (`.chat-input`) as now; typing there starts the chat without touching the
  item.
- **Edit, then reply.** Clicking the reply field right after an edit saves the item and waits for
  one more message before anything goes to the AI, so an edit never fires an exchange by itself.

## Messages

- **Styles.** The user's messages (`.chat-msg.user`) are styled as the description text, left
  aligned on the panel background; the AI's replies (`.chat-msg.ai`) are aligned right and
  highlighted — the roles switch one message style, nothing more.
- **Tools.** Every reply but the first (the content) carries at its left edge, level with its
  first line (`.msg-tools`):
  - a collapse / expand button (`.msg-fold`, `aria-expanded`) folding the message to its first line;
  - a check mark (`.msg-keep`, `role="checkbox"`, on by default); switched off, the message is
    dropped from the exchange temporarily — its text turns grey (`.chat-msg.dropped`) and it does
    not travel with the next turn until switched on again;
  - two buttons that turn the message into a syngate item: `.msg-child` adds it as a child of
    the anchored item, `.msg-sibling` as its next sibling. Both go through the editor's own
    `add_item`; the message's first line is the header, the rest the description. The chat stays
    as it is after the transform: the new item is extracted from it, not replaced by it.
- **Items as references.** A chat message names an item the way a file is named with `@`: the
  harness resolves every `@UID` into the item's statement for the turn, appended to the seed
  context as one more section (`@UID header`, then the description), the message text staying as
  typed.
- **What travels.** A turn resumes the connector's session, so the exchange itself stays with the
  connector. Switching a check mark drops the session: the next turn starts a new one, headed by
  the transcript of the kept messages, so a dropped message is really out and a kept one back in.

## Waiting

While a turn runs — a single answer, or one awaiting the subagents or the workflow it launched —
the log ends with a spinner note and the green check mark is a `■ stop` button (`.chat-stop`): the
chat can be neither closed nor collapsed until the turn ends. Stop interrupts the connector's
process; the calls the turn applied so far stay applied, and the log shows the turn as stopped.
The turn itself runs on the server, so a reload of the page re-attaches to it and shows its reply
when it comes.

## History

The history lives in memory. The green check mark (`.chat-close`) hides the chat and becomes a
drop-down icon (`.chat-reopen`, `▾`) on the bottom-left border that opens the saved history again.
While the chat is hidden the panel behaves as if no chat existed: `✦ AI chat` stays offered, and a
new chat started there replaces the hidden one at the moment its first message is sent to the AI,
not before — until then the drop-down still reopens the old history.
