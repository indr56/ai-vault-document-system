/** Splits an answer into text, bold and citation pieces for rendering.
 *
 * Bold is tracked across citation marks, so "**$18,650 [1] - $15,000 [2] = $3,650**" stays bold
 * around its citation badges (splitting at the marks first used to leave the "**" on screen).
 * An unpaired "**" is kept as plain text.
 */
export type AnswerPiece =
  | { kind: "text"; text: string; bold: boolean }
  | { kind: "cite"; number: number; mark: string; bold: boolean };

export function answerPieces(content: string): AnswerPiece[] {
  const tokens = content.split(/(\*\*|\[\d{1,2}\])/g).filter((t) => t !== "");
  const markers = tokens.filter((t) => t === "**").length;
  const pieces: AnswerPiece[] = [];
  let bold = false;
  let seen = 0;
  for (const token of tokens) {
    if (token === "**") {
      seen += 1;
      if (!bold && seen === markers && markers % 2 === 1) {
        pieces.push({ kind: "text", text: token, bold }); // the one unpaired marker: show as written
      } else {
        bold = !bold;
      }
      continue;
    }
    const cite = token.match(/^\[(\d{1,2})\]$/);
    pieces.push(cite ? { kind: "cite", number: Number(cite[1]), mark: token, bold } : { kind: "text", text: token, bold });
  }
  return pieces;
}
