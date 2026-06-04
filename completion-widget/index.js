import DjangoQL from 'djangoql-completion';

import 'djangoql-completion/dist/completion.css';

// When a suggestion is picked, the upstream widget writes the new value
// straight to `textarea.value`. Assigning `.value` does NOT fire an `input`
// event, so anything listening for one — notably the highlight overlay
// (highlight.js repaints its coloured backdrop on `input`) — never learns the
// text changed. The result is invisible "white-on-white" text for everything
// chosen from the popup, until the next keystroke repaints it.
//
// Fix it at the single source every integration shares: dispatch a bubbling
// `input` event after `selectCompletion` runs, exactly as real typing would.
var selectCompletion = DjangoQL.prototype.selectCompletion;
DjangoQL.prototype.selectCompletion = function () {
  selectCompletion.apply(this, arguments);
  if (this.textarea) {
    var event;
    try {
      event = new Event('input', { bubbles: true });
    } catch (e) {
      // Legacy browsers without the Event constructor (e.g. IE).
      event = document.createEvent('Event');
      event.initEvent('input', true, false);
    }
    this.textarea.dispatchEvent(event);
  }
};

// Object-picker value suggestions arrive as "Label #<id>" (the id is the filter
// key). Render that trailing " #<id>" as a muted, display-only part so it reads
// as metadata, not a count — the inserted value (s.text) keeps the id, so pk
// filtering is unaffected.
var populateFieldOptions = DjangoQL.prototype.populateFieldOptions;
DjangoQL.prototype.populateFieldOptions = function () {
  populateFieldOptions.apply(this, arguments);
  var suggestions = this.suggestions || [];
  for (var i = 0; i < suggestions.length; i++) {
    var s = suggestions[i];
    if (s && typeof s.suggestionText === 'string') {
      var m = /^(.*\S)\s+(#\d+)$/.exec(s.suggestionText);
      if (m) {
        s.suggestionText = m[1] + ' <i>' + m[2] + '</i>';
      }
    }
  }
};

window.DjangoQL = DjangoQL;
