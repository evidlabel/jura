// The CodeMirror pieces the Write page uses, on window.CM (an IIFE bundle: the page has no module loader).
import { EditorState, Compartment, StateField, StateEffect, RangeSetBuilder, Prec, Transaction, EditorSelection } from '@codemirror/state';
import { EditorView, ViewPlugin, Decoration, keymap, drawSelection, highlightActiveLine, rectangularSelection, dropCursor } from '@codemirror/view';
import { history, historyKeymap, defaultKeymap, undo, redo, undoDepth, redoDepth, indentWithTab, isolateHistory } from '@codemirror/commands';
import { foldService, codeFolding, foldEffect, unfoldEffect, foldedRanges, indentUnit } from '@codemirror/language';
import { autocompletion, completionKeymap, startCompletion, completionStatus } from '@codemirror/autocomplete';
import { vim, Vim, getCM } from '@replit/codemirror-vim';

window.CM = {
  EditorState, Compartment, StateField, StateEffect, RangeSetBuilder, Prec, Transaction, EditorSelection,
  EditorView, ViewPlugin, Decoration, keymap, drawSelection, highlightActiveLine, rectangularSelection, dropCursor,
  history, historyKeymap, defaultKeymap, undo, redo, undoDepth, redoDepth, indentWithTab, isolateHistory,
  foldService, codeFolding, foldEffect, unfoldEffect, foldedRanges, indentUnit,
  autocompletion, completionKeymap, startCompletion, completionStatus,
  vim, Vim, getCM,
};
