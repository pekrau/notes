"Simple app for personal notebooks stored in the file system."

__version__ = "0.8.7"

import collections
import json
import os
import time

import flask
import marko
import marko.ast_renderer
import jinja2.utils


ROOT = None           # The root note. Created in 'setup'.
STARRED = set()       # Starred notes.
RECENT = None         # Deque of recently modified note. Created in 'setup'.
BACKLINKS = dict()    # Lookup of target note path -> set of source note paths.
HASHTAGS = dict()     # Lookup of word -> set of note paths.


def get_settings():
    "Return the settings."
    settings = dict(VERSION = __version__,
                    SERVER_NAME = "localhost.localdomain:5099",
                    SECRET_KEY = "this is a secret key",
                    TEMPLATES_AUTO_RELOAD = True,
                    DEBUG = True,
                    JSON_AS_ASCII = False,
                    IMAGE_EXTENSIONS = [".png",".jpg",".jpeg",".svg",".gif"],
                    MAX_RECENT = 12,
                    NOTEBOOKS = [],
                    DEFAULT_NOTEBOOK = "example")
    dirpath = os.path.dirname(__file__)
    filepath = os.path.join(dirpath, "settings.json")
    try:
        with open(filepath) as infile:
            settings.update(json.load(infile))
    except OSError:
        pass
    settings["SETTINGS_FILEPATH"] = filepath
    # Add the default notebook if none recorded since before.
    if not settings["NOTEBOOKS"]:
        settings["NOTEBOOKS"].append(os.path.join(dirpath,
                                                  settings["DEFAULT_NOTEBOOK"]))
    # The first notebook is the starting one.
    settings["NOTEBOOK_DIRPATH"] = settings["NOTEBOOKS"][0]
    settings["NOTEBOOK_TITLE"] = os.path.basename(settings["NOTEBOOK_DIRPATH"])
    return settings

def write_settings():
    """Write out the settings file with updated information.
    Update only the information that can be changed via the app.
    """
    dirpath = os.path.dirname(__file__)
    filepath = os.path.join(dirpath, "settings.json")
    try:
        with open(filepath) as infile:
            settings = json.load(infile)
    except OSError:
        settings = {}
    with open(filepath, "w") as outfile:
        for key in ["NOTEBOOKS"]:
            settings[key] = flask.current_app.config[key]
        json.dump(settings, outfile, indent=2)


class Note:
    "Note and its subnotes, if any."

    def __init__(self, supernote, title):
        self.supernote = supernote
        if supernote:
            supernote.subnotes.append(self)
        self.subnotes = []
        self._title = title
        self._text = ""
        self._ast = None
        self.file_extension = None

    def __repr__(self):
        return self.path

    def __lt__(self, other):
        return self.title < other.title

    def __contains__(self, term):
        "Does this note contain the search term?"
        term = term.lower()
        if term in self.title.lower(): return True
        if term in self.text.lower(): return True
        return False

    def get_title(self):
        return self._title

    def set_title(self, title):
        """Set a new title, which changes its path.
        Updates notes that link to this note or its subnotes.
        Raise ValueError if the title is invalid; bad start or end characters.
        Raise KeyError if there is already a note with that title
        """
        if not self.supernote: return  # Root note has no title to change.
        title = title.replace("\r", "")
        title = title.strip()
        if not title: raise ValueError
        if "/" in title: raise ValueError
        if title[0] == ".": raise ValueError
        if title[0] == "_": raise ValueError
        if title[-1] == "~": raise ValueError
        if self.title == title: return
        new_abspath = os.path.join(flask.current_app.config["NOTEBOOK_DIRPATH"],
                                   self.supernote.path,
                                   title)
        if os.path.exists(new_abspath): raise KeyError
        if os.path.exists(new_abspath + ".md"): raise KeyError
        # The set of notes whose paths will change: this one and all below it.
        changing = list(self.traverse())
        # Remember the old path for each note whose paths will change.
        old_paths = [note.path for note in changing]
        # The set of notes which link to any of the changing-path notes.
        linking = set()
        for note in changing:
            try:
                linking.update(BACKLINKS[note.path])
            except KeyError:
                pass
        linking = [get_note(path) for path in linking]
        # Remove all backlinks and hashtags while old paths.
        for note in linking:
            note.remove_backlinks()
            note.remove_hashtags()
        # Old abspath needed for renaming directory/file.
        old_abspath = self.abspath
        # Save file path for any attached file.
        if self.file_extension:
            old_abspathfile = self.abspathfile
        # Actually change the title of the note; rename the file/directory.
        self._title = title
        if os.path.isdir(old_abspath):
            abspath = self.abspath
            os.rename(old_abspath, abspath)
        else:
            abspath = self.abspath + ".md"
            os.rename(old_abspath + ".md", abspath)
        # Rename the attached file if there is one.
        if self.file_extension:
            os.rename(old_abspathfile, self.abspathfile)
        # Force the modified timestamp of the file to now.
        self.set_modified()
        # Get the new path for each note whose path was changed.
        changed_paths = zip(old_paths, [note.path for note in changing])
        for note in linking:
            text = note.text
            for old_path, new_path in changed_paths:
                text = text.replace(f"[[{old_path}]]", f"[[{new_path}]]")
            note._text = text   # Do not add backlinks just yet.
            note._ast = None    # Force recompile of AST.
            note.write(update_modified=False)
        # Add back backlinks and hashtags with new the paths.
        for note in linking:
            note.add_backlinks()
            note.add_hashtags()
        check_backlinks()

    title = property(get_title, set_title, doc="The title of the note.")

    def get_text(self):
        return self._text

    def set_text(self, text):
        text = text.replace("\r", "")
        self.remove_backlinks()
        self.remove_hashtags()
        self._text = text
        self._ast = None        # Force recompile of AST.
        self.add_backlinks()
        self.add_hashtags()
        self.write()
        check_backlinks()

    text = property(get_text, set_text,
                    doc="The text of the note using Markdown format.")

    def get_modified(self):
        if self.subnotes:
            return os.path.getmtime(os.path.join(self.abspath, "__text__.md"))
        else:
            return os.path.getmtime(self.abspath + ".md")

    def set_modified(self, value=None):
        if value is None:
            value = time.time()
        if self.subnotes:
            os.utime(os.path.join(self.abspath, "__text__.md"), (value, value))
        else:
            os.utime(self.abspath + ".md", (value, value))

    modified = property(get_modified, set_modified,
                        doc="The modification timestamp of the note.")

    @property
    def ast(self):
        "Set the private variable to None to force recompile of the AST."
        if self._ast is None:
            self._ast = get_md_ast_parser().convert(self.text)
        return self._ast

    @property
    def path(self):
        "Return the path of the note."
        if self.supernote:
            assert self.title
            return os.path.join(self.supernote.path, self.title)
        else:
            return self.title or ""

    @property
    def idpath(self):
        "Return the path of the note as a valid HTML code identifier."
        return self.path.replace("/", "-").replace(" ", "_").replace(",", "_")

    @property
    def abspath(self):
        "Return the absolute filepath of the note."
        path = self.path
        if path:
            return os.path.join(
                flask.current_app.config["NOTEBOOK_DIRPATH"], path)
        else:
            return flask.current_app.config["NOTEBOOK_DIRPATH"]

    @property
    def abspathfile(self):
        if not self.file_extension:
            raise ValueError("No file attached to this note.")
        return self.abspath + self.file_extension

    @property
    def url(self):
        if self.path:
            return flask.url_for("note", path=self.path)
        else:
            return flask.url_for("home")

    @property
    def file(self):
        return bool(self.file_extension)

    @property
    def file_size(self):
        return os.path.getsize(self.abspathfile)

    @property
    def count(self):
        "Return the number of subnotes."
        return len(self.subnotes)

    @property
    def starred(self):
        "Is the note starred?"
        return self in STARRED

    def star(self, remove=False):
        "Toggle the star state of the note, or force remove."
        if self in STARRED:
            STARRED.remove(self)
        elif not remove:
            STARRED.add(self)
        else:
            return              # No change; no need to update file.
        filepath = os.path.join(flask.current_app.config["NOTEBOOK_DIRPATH"],
                                "__starred__.json")
        with open(filepath, "w") as outfile:
            json.dump({"paths": [n.path for n in STARRED]}, outfile)

    def put_recent(self):
        "Put the note to the start of the list of recently modified notes."
        # Root note should not be listed.
        if self.supernote is None: return
        self.remove_recent()
        RECENT.appendleft(self)
        check_recent_ordered()

    def remove_recent(self):
        "Remove this note from the list of recently modified notes."
        try:
            RECENT.remove(self)
        except ValueError:
            pass

    def count_traverse(self):
        "Return the number of subnotes recursively, including this one."
        result = 1
        for subnote in self.subnotes:
            result += subnote.count_traverse()
        return result

    def supernotes(self):
        "Return the list of supernotes."
        if self.supernote:
            return self.supernote.supernotes() + [self.supernote]
        else:
            return []

    def traverse(self):
        "Return a generator traversing this note and its subnotes."
        yield self
        for subnote in self.subnotes:
            yield from subnote.traverse()

    def write(self, update_modified=True):
        "Write this note to disk. Does *not* write subnotes."
        if os.path.isdir(self.abspath):
            abspath = os.path.join(self.abspath, "__text__.md")
            try:
                stat = os.stat(abspath)
            except OSError:     # Fallback to directory if no text file.
                stat = os.stat(self.abspath)
            with open(abspath, "w") as outfile:
                outfile.write(self.text)
        else:
            abspath = self.abspath + ".md"
            try:
                stat = os.stat(abspath)
            except OSError:     # If the note is new, then no such file.
                stat = None
            with open(abspath, "w") as outfile:
                outfile.write(self.text)
        if not update_modified and stat:
            os.utime(abspath, (stat.st_atime, stat.st_mtime))

    def upload_file(self, content, extension):
        """Upload the file given by content and filename extension.
        Does NOT change the modified timestamp, nor puts the note into
        the list of recently modified notes.
        NOTE: This is based on the assumption that the note was also
        modified in some other way in the same operation.
        """
        if self is ROOT:
            raise ValueError("Cannot attach a file to the root note.")
        # Uploading Markdown files would create havoc.
        if extension == ".md":
            raise ValueError("Upload of '.md' files is not allowed.")
        # Non-extension upload must have some extension.
        elif not extension:
            extension = ".bin"
        # Remove any existing attached file; may have different extension
        if self.file_extension:
            os.remove(self.abspathfile)
        filepath = os.path.join(self.supernote.abspath, self.title + extension)
        with open(filepath, "wb") as outfile:
            outfile.write(content)
        self.file_extension = extension
        self.file_size = len(content)

    def remove_file(self):
        "Remove the attached file, if any."
        if not self.file_extension: return
        os.remove(self.abspathfile)
        self.file_extension = None
        self.file_size = None

    def read(self):
        "Read this note and its subnotes from disk."
        if os.path.exists(self.abspath):
            # It's a directory with subnotes.
            try:
                filepath = os.path.join(self.abspath, "__text__.md")
                with open(filepath) as infile:
                    self._text = infile.read()
            except OSError:           # No text file for directory.
                self._text = ""
                 # Fallback: Use directory's timestamp!
                stat = os.stat(self.abspath)
                # Create an empty text file.
                filepath = os.path.join(self.abspath, "__text__.md")
                with open(filepath, "w") as outfile:
                    outfile.write("")
                os.utime(filepath, (stat.st_atime, stat.st_mtime))
            self._ast = None          # Force recompile of AST.
            self._files = {}          # Needed only during read of subnotes.
            basenames = []
            for filename in sorted(os.listdir(self.abspath)):
                if filename.startswith("_"): continue
                if filename.endswith("~"): continue
                basename, extension = os.path.splitext(filename)
                # Proper note file; handle once directory listing is done.
                if not extension or extension == ".md":
                    basenames.append(basename)
                # Record attached files for later.
                else:
                    self._files[basename] = extension
            # Actually read subnotes when all files have been cycled over.
            for basename in basenames:
                note = Note(self, basename)
                note.read()
            del self._files     # No longer needed.
        else:
            # It's a file; no subnotes.
            filepath = self.abspath + ".md"
            with open(filepath) as infile:
                self._text = infile.read()
                self._ast = None      # Remove the AST cache.
        # Both directory (except root) and file note may have
        # an attachment, which would be a single file at the
        # same level with the same name, but a non-md extension.
        if self.supernote:
            # Attached files recorded in supernote.
            try:
                self.file_extension = self.supernote._files[self.title]
            except KeyError:
                pass

    def get_backlinks(self):
        "Get the notes linking to this note."
        return sorted([get_note(p) for p in BACKLINKS.get(self.path, [])])

    def add_backlinks(self):
        "Add the links to other notes in this note to the lookup."
        path = self.path
        for link in self.parse_linkpaths(self.ast["children"]):
            try:
                get_note(link)
            except KeyError:
                pass
            else:
                BACKLINKS.setdefault(link, set()).add(path)

    def remove_backlinks(self):
        "Remove the links to other notes in this note from the lookup."
        linkpaths = self.parse_linkpaths(self.ast["children"])
        path = self.path
        for link in linkpaths:
            try:
                BACKLINKS[link].remove(path)
            except KeyError:    # When stale link.
                pass

    def parse_linkpaths(self, children):
        """Find the note links in the children of the AST tree.
        Return the set of paths for the notes linked to.
        """
        result = set()
        if isinstance(children, list):
            for child in children:
                if child.get("element") == "note_link":
                    result.add(child["ref"])
                try:
                    result.update(self.parse_linkpaths(child["children"]))
                except KeyError:
                    pass
        return result

    def add_hashtags(self):
        "Add the hashtags in this note to the lookup."
        path = self.path
        for word in self.parse_hashtags(self.ast["children"]):
            HASHTAGS.setdefault(word, set()).add(path)

    def remove_hashtags(self):
        "Remove the hashtags in this note from the lookup."
        path = self.path
        for word in self.parse_hashtags(self.ast["children"]):
            try:
                HASHTAGS[word].remove(path)
            except KeyError:
                pass
            else:
                if not HASHTAGS[word]:
                    HASHTAGS.pop(word)

    def parse_hashtags(self, children):
        """Find the hashtags in the children of the AST tree.
        Return the set of words.
        """
        result = set()
        if isinstance(children, list):
            for child in children:
                if child.get("element") == "hash_tag":
                    result.add(child["word"])
                try:
                    result.update(self.parse_hashtags(child["children"]))
                except KeyError:
                    pass
        return result

    def create_subnote(self, title, text):
        "Create and return a subnote."
        for subnote in self.subnotes:
            if title == subnote.title:
                raise ValueError(f"Note already exists: '{title}'")
        # If this note is a file, then convert it into a directory.
        absfilepath = self.abspath + ".md"
        if os.path.isfile(absfilepath):
            os.mkdir(self.abspath)
            os.rename(absfilepath, os.path.join(self.abspath, "__text__.md"))
        note = Note(self, title)
        self.subnotes.sort()
        # Set the text of the subnote; this also adds backlinks.
        note.text = text
        note.write()
        note.put_recent()
        return note

    def move(self, supernote):
        "Move this note to the given supernote."
        # The set of notes whose paths will change: this one and all below it.
        changing = list(self.traverse())
        if supernote in changing:
            raise ValueError("Cannot move note to itself or one of its subnotes.")
        for note in supernote.subnotes:
            if self.title == note.title:
                raise ValueError("New supernote already has a subnote"
                                 " with the title of this note.")
        # Remember the old path for all notes whose paths will change.
        old_paths = [note.path for note in changing]
        # The set of notes which link to any of the changing-path notes.
        linking = set()
        for note in changing:
            try:
                linking.update(BACKLINKS[note.path])
            except KeyError:
                pass
        linking = [get_note(path) for path in linking]
        # Remove all backlinks and hashtags while old paths.
        for note in changing:
            note.remove_backlinks()
            note.remove_hashtags()
        for note in linking:
            note.remove_backlinks()
            note.remove_hashtags()
        # Old abspath needed for renaming directory/file.
        old_abspath = self.abspath
        # Save file path for any attached file.
        if self.file_extension:
            old_abspathfile = self.abspathfile
        # If the new supernote is a file, then convert it first to a directory.
        super_absfilepath = supernote.abspath + ".md"
        if os.path.isfile(super_absfilepath):
            os.mkdir(supernote.abspath)
            os.rename(super_absfilepath,
                      os.path.join(supernote.abspath, "__text__.md"))
        # Remember old supernote.
        old_supernote = self.supernote
        # Actually set the new supernote; move the file/directory of the note.
        old_supernote.subnotes.remove(self)
        self.supernote = supernote
        self.supernote.subnotes.append(self)
        self.supernote.subnotes.sort()
        if os.path.isdir(old_abspath):
            new_abspath = self.abspath
            os.rename(old_abspath, new_abspath)
        else:
            new_abspath = self.abspath + ".md"
            os.rename(old_abspath + ".md", new_abspath)
        # Move the attached file if there is one.
        if self.file_extension:
            os.rename(old_abspathfile, self.abspathfile)
        # Convert the old supernote to file, if no other subnotes in it.
        if len(old_supernote.subnotes) == 0:
            old_supernote_fileabspath = old_supernote.abspath + ".md"
            with open(old_fileabspath, "w") as outfile:
                outfile.write(old_supernote.text)
        # Get the new path for each note whose path was changed.
        changed_paths = zip(old_paths, [note.path for note in changing])
        for note in linking:
            text = note.text
            for old_path, new_path in changed_paths:
                text = text.replace(f"[[{old_path}]]", f"[[{new_path}]]")
            note._text = text   # Do not add backlinks just yet.
            note._ast = None    # Force recompile.
            note.write(update_modified=False)
        # Add back backlinks and hashtags with new the paths.
        for note in changing:
            note.add_backlinks()
            note.add_hashtags()
        for note in linking:
            note.add_backlinks()
            note.add_hashtags()
        # Add this note to recently changed.
        self.put_recent()

    def is_deletable(self):
        """May this note be deleted?
        - Must have no subnotes.
        - Must have no links to it.
        """
        if self.supernote is None: return False
        if self.count: return False
        if self.get_backlinks(): return False
        return True

    def delete(self):
        "Delete this note."
        if not self.is_deletable():
            raise ValueError("This note may not be deleted.")
        self.remove_backlinks()
        self.remove_hashtags()
        self.star(remove=True)
        self.remove_recent()
        os.remove(self.abspath + ".md")
        if self.file_extension:
            os.remove(self.abspathfile)
        self.supernote.subnotes.remove(self)
        # Convert supernote to file if no subnotes any longer. Not root!
        if self.supernote.count == 0 and self.supernote is not None:
            abspath = self.supernote.abspath
            filepath = os.path.join(abspath, "__text__.md")
            try:
                os.rename(filepath, abspath + ".md")
            except OSError:     # May happen if e.g. no text for dir.
                with open(abspath + ".md", "w") as outfile:
                    outfile.write(sels.supernote.text)
            os.rmdir(abspath)

    def check_synced_filesystem(self):
        "When DEBUG: Check that this note is synced with its storage on disk."
        if not flask.current_app.config["DEBUG"]: return
        flask.current_app.logger.debug(f"check_synced_filesystem '{self}'")
        if self.subnotes:
            if not os.path.isdir(self.abspath):
                raise ValueError(f"'{self}' contains subnotes but is not a directory")
            try:
                with open(os.path.join(self.abspath, "__text__.md")) as infile:
                    text = infile.read()
            except OSError:
                text = ""
        else:
            abspath = self.abspath + ".md"
            if not os.path.isfile(abspath):
                raise ValueError(f"{self} has no subnotes but is not a file")
            with open(abspath) as infile:
                text = infile.read()
        if text != self.text:
            raise ValueError(f"'{self}' text differs from file")


class Timer:
    "CPU timer, wall-clock timer."

    def __init__(self):
        self.cpu_start = time.process_time()
        self.wallclock_start = time.time()

    def __str__(self):
        return f"CPU time: {self.cpu_time:.3f} s, wall-clock time: {self.wallclock_time:.3f} s"

    @property
    def cpu_time(self):
        "Return CPU time (in seconds) since start of this timer."
        return time.process_time() - self.cpu_start

    @property
    def wallclock_time(self):
        "Return wall-clock time (in seconds) since start of this timer."
        return time.time() - self.wallclock_start


class NoteLink(marko.inline.InlineElement):
    pattern = r'\[\[ *(.+?) *\]\]'
    parse_children = False
    def __init__(self, match):
        self.ref = match.group(1)

class NoteLinkRendererMixin:
    def render_note_link(self, element):
        try:
            note = get_note(element.ref)
        except KeyError:
            # Stale link; target does not exist.
            return f'<span class="text-danger">{element.ref}</span>'
        else:
            # Proper link to target.
            return f'<a class="fw-bold text-decoration-none" href="{note.url}">{note.title}</a>'

class NoteLinkExt:
    elements = [NoteLink]
    renderer_mixins = [NoteLinkRendererMixin]

class HashTag(marko.inline.InlineElement):
    pattern = r'#([^#].+?)\b'
    parse_children = False
    def __init__(self, match):
        self.word = match.group(1)

class HashTagRendererMixin:
    def render_hash_tag(self, element):
        url = flask.url_for("hashtag", word=element.word)
        return f'<a class="fst-italic text-decoration-none" href="{url}">#{element.word}</a>'

class HashTagExt:
    elements = [HashTag]
    renderer_mixins = [HashTagRendererMixin]

class BareUrl(marko.inline.InlineElement):
    pattern = r'(https?://\S+)'
    parse_children = False
    def __init__(self, match):
        self.url = match.group(1)

class BareUrlRendererMixin:
    def render_bare_url(self, element):
        return f'<a class="text-decoration-none" href="{element.url}">{element.url}</a>'

class BareUrlExt:
    elements = [BareUrl]
    renderer_mixins = [BareUrlRendererMixin]

class HTMLRenderer(marko.html_renderer.HTMLRenderer):
    "Fix various output for Bootstrap."

    def render_quote(self, element):
        return '<blockquote class="blockquote">\n{}</blockquote>\n'.format(self.render_children(element))

def get_md_parser():
    return marko.Markdown(extensions=[NoteLinkExt, HashTagExt, BareUrlExt],
                          renderer=HTMLRenderer)

def get_md_ast_parser():
    return marko.Markdown(extensions=[NoteLinkExt, HashTagExt, BareUrlExt],
                          renderer=marko.ast_renderer.ASTRenderer)

def markdown(value):
    "Filter to process the value using augmented Marko markdown."
    return jinja2.utils.Markup(get_md_parser().convert(value or ""))

def localtime(value):
    "Filter to convert epoch value to local time ISO string."
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))

def flash_error(msg): flask.flash(str(msg), "error")

def flash_warning(msg): flask.flash(str(msg), "warning")

def flash_message(msg): flask.flash(str(msg), "message")

def get_note(path):
    "Get the note given its path."
    if not path: return ROOT
    note = ROOT
    parts = list(reversed(path.split("/")))
    while parts:
        title = parts.pop()
        for subnote in note.subnotes:
            if title == subnote.title:
                note = subnote
                break
        else:
            raise KeyError(f"No such note '{path}'.")
    return note

def get_starred(): return sorted(STARRED)

def get_recent(): return list(RECENT)

def get_hashtags(): return sorted(HASHTAGS.keys())

def check_recent_ordered():
    "When DEBUG: If RECENT is not ordered, print it and raise ValueError."
    if not flask.current_app.config["DEBUG"]: return
    flask.current_app.logger.debug("checked_recent_ordered")
    latest = RECENT[0]
    for note in RECENT:
        if note.modified > latest.modified:
            content = "\n".join([f"{localtime(n.modified)}  {n}" for n in RECENT])
            raise ValueError(f"RECENT out of order:\n{content}")

def check_synced_filesystem():
    "When DEBUG: Check that all notes in memory exist as files/directories."
    if not flask.current_app.config["DEBUG"]: return
    flask.current_app.logger.debug("checked_synced_filesystem for all notes")
    for note in ROOT.traverse():
        note.check_synced_filesystem()

def check_synced_memory():
    "When DEBUG: Check that files/directories exist as notes in memory."
    if not flask.current_app.config["DEBUG"]: return
    flask.current_app.logger.debug("checked_synced_memory for all notes")
    root = flask.current_app.config["NOTEBOOK_DIRPATH"]
    try:
        abspath = os.path.join(root, "__text__.md")
        with open(abspath) as infile:
            text = infile.read()
    except OSError:
        text = ""
    if text != ROOT.text:
        raise ValueError(f"file {abspath} text differs from ROOT")
    for dirpath, dirnames, filenames in os.walk(root):
        for dirname in dirnames:
            abspath = os.path.join(dirpath, dirname)
            path = abspath[len(root)+1:]
            try:
                note = get_note(path)
            except KeyError:
                raise ValueError(f"No note for directory {abspath}")
            if not note.subnotes:
                raise ValueError(f"Directory {abspath} note has no subnotes")
            try:
                textabspath = os.path.join(abspath, "__text__.md")
                with open(textabspath) as infile:
                    text = infile.read()
            except OSError:
                text = ""
            if text != note.text:
                raise ValueError(f"file {textabspath} text differs from '{note}'")
                
        for filename in filenames:
            abspath = os.path.join(dirpath, filename)
            basename, ext = os.path.splitext(filename)
            if basename.startswith("_"): continue
            path = os.path.join(dirpath, basename)[len(root)+1:]
            if ext == ".md":
                try:
                    note = get_note(path)
                except KeyError:
                    raise ValueError(f"No note for file {abspath}")
                with open(abspath) as infile:
                    text = infile.read()
                if text != note.text:
                    raise ValueError(f"file {abspath} text differs from '{note}'")
            else:
                try:
                    note = get_note(path)
                except KeyError:
                    raise ValueError(f"No note '{path}' for non-md file {abspath}")
                if ext != note.file_extension:
                    raise ValueError(f"Non-md file {abspath} extension"
                                     f" does not match '{note}'")
                if os.path.getsize(abspath) != note.file_size:
                    raise ValueError(f"Non-md file {abspath} size"
                                     f" does not match '{note}'")

def check_backlinks():
    "Check that paths in backlinks refer to existing notes."
    for source, targets in BACKLINKS.items():
        try:
            get_note(source)
        except KeyError:
            raise ValueError(f"backlink source '{source}' does not resolve")
        for target in targets:
            try:
                get_note(target)
            except KeyError:
                raise ValueError(f"backlink target '{target}' does not resolve")


app = flask.Flask(__name__)

app.add_template_filter(markdown)
app.add_template_filter(localtime)

@app.before_first_request
def setup():
    """Read all notes and keep in memory. Set up:
    - List of recent notes
    - List of starred notes
    - Set up map of backlinks
    - Set up map of hashtags
    """
    timer = Timer()
    global ROOT
    global RECENT
    STARRED.clear()
    BACKLINKS.clear()
    HASHTAGS.clear()
    # Read in all notes.
    ROOT = Note(None, None)
    ROOT.read()
    # Set up most recently modified notes.
    traverser = ROOT.traverse()
    next(traverser)             # Skip root note.
    notes = list(traverser)     # XXX simple but not very good.
    notes.sort(key=lambda n: n.modified, reverse=True)
    RECENT = collections.deque(notes[:flask.current_app.config["MAX_RECENT"]],
                               maxlen=flask.current_app.config["MAX_RECENT"])
    # Get the starred notes.
    try:
        filepath = os.path.join(flask.current_app.config["NOTEBOOK_DIRPATH"], 
                                "__starred__.json")
        with open(filepath) as infile:
            for path in json.load(infile)["paths"]:
                try:
                    STARRED.add(get_note(path))
                except KeyError:
                    pass
    except OSError:
        pass
    # Set up the backlinks and hashtags for all notes.
    for note in ROOT.traverse():
        note.add_backlinks()
        note.add_hashtags()
    check_recent_ordered()
    check_synced_filesystem()
    check_synced_memory()
    check_backlinks()
    flash_message(f"Setup {timer}")

@app.context_processor
def setup_template_context():
    "Add to the global context of Jinja2 templates."
    return dict(interactive=True,
                flash_error=flash_error,
                flash_warning=flash_warning,
                flash_message=flash_message,
                get_starred=get_starred,
                get_recent=get_recent,
                get_hashtags=get_hashtags)

@app.before_request
def prepare():
    "Add the config dictionary to the 'g' object."
    flask.g.config = flask.current_app.config

@app.route("/")
def home():
    "Home page; root note of the current notebook."
    n_links = sum([len(s) for s in BACKLINKS.values()])
    notebooks = [(os.path.basename(n), n) 
                 for n in flask.current_app.config["NOTEBOOKS"]]
    return flask.render_template("home.html", 
                                 root=ROOT,
                                 n_links=n_links,
                                 notebooks=notebooks)

@app.route("/note")
@app.route("/note/")
def root():
    "Root note of the current notebook; redirect to home."
    return flask.redirect(flask.url_for("home"))

@app.route("/create", methods=["GET", "POST"])
def create():
    "Create a new note, optionally with an uploaded file."
    if flask.request.method == "GET":
        try:
            supernote = get_note(flask.request.values["supernote"])
        except KeyError:
            supernote = None    # Root supernote.
        try:
            source = get_note(flask.request.values["source"])
        except KeyError:
            source = None
        return flask.render_template("create.html",
                                     supernote=supernote,
                                     source=source,
                                     upload=flask.request.values.get("upload"))

    elif flask.request.method == "POST":
        try:
            superpath = flask.request.form["supernote"]
            if not superpath: raise KeyError
        except KeyError:
            supernote = ROOT
        else:
            try:
                supernote = get_note(superpath)
            except KeyError:
                flash_error(f"No such supernote: '{superpath}'")
                return flask.redirect(flask.url_for("home"))
        upload = flask.request.files.get("upload")
        if upload:
            title, extension = os.path.splitext(upload.filename)
        else:
            title = flask.request.form.get("title") or "No title"
        title = title.replace("\n", " ")  # Clean up title.
        title = title.replace("/", " ")   # Avoid confusion with subnotes.
        title = title.strip()
        title = title.replace(".", "_")   # Avoid confusion with extensions.
        title = title.lstrip("_")         # Avoid confusion with system files.
        text = flask.request.form.get("text") or ""
        try:
            note = supernote.create_subnote(title, text)
            if upload:
                note.upload_file(upload.read(), extension)
        except ValueError as error:
            flash_error(error)
            return flask.redirect(supernote.url)
        check_recent_ordered()
        check_synced_filesystem()
        check_synced_memory()
        check_backlinks()
        return flask.redirect(note.url)

@app.route("/note/<path:path>")
def note(path):
    "Display page for the given note."
    try:
        note = get_note(path)
    except KeyError:
        flash_error(f"No such note: '{path}'")
        return flask.redirect(flask.url_for("note", path=os.path.dirname(path)))
    return flask.render_template("note.html", note=note)

@app.route("/file/<path:path>")
def file(path):
    "Return the file for the given note. Optionally for download."
    try:
        note = get_note(path)
    except KeyError:
        flash_error(f"No such note: '{path}'")
        return flask.redirect(flask.url_for("note", path=os.path.dirname(path)))
    if not note.file_extension:
        raise KeyError(f"No file attached to note '{path}'")
    if flask.request.values.get("download"):
        return flask.send_file(note.abspathfile,
                               conditional=False,
                               as_attachment=True)
    else:
        return flask.send_file(note.abspathfile, conditional=False)

@app.route("/edit/", methods=["GET", "POST"])
@app.route("/edit/<path:path>", methods=["GET", "POST"])
def edit(path=""):
    "Edit the given note; title (i.e. file/directory rename) and/or text."
    try:
        note = get_note(path)
    except KeyError:
        if not path:
            note = ROOT
        else:
            flash_error(f"No such note: '{path}'")
            return flask.redirect(
                flask.url_for("note", path=os.path.dirname(path)))

    if flask.request.method == "GET":
        return flask.render_template("edit.html", note=note)

    elif flask.request.method == "POST":
        try:
            title = flask.request.form.get("title") or ""
            note.title = title
        except ValueError as error:
            flash_error(f"Invalid title: '{title}'")
            return flask.redirect(flask.url_for("edit", path=path))
        except KeyError as error:
            flash_error(f"Note already exists: '{title}'")
            return flask.redirect(flask.url_for("edit", path=path))
        # Actually change text; should update links, etc.
        note.text = flask.request.form.get("text") or ""
        # Handle file remove or upload.
        if flask.request.form.get("removefile"):
            note.remove_file()
        else:
            upload = flask.request.files.get("upload")
            if upload:
                note.upload_file(upload.read(),
                                 os.path.splitext(upload.filename)[1])      
        note.put_recent()
        check_recent_ordered()
        check_synced_filesystem()
        check_synced_memory()
        check_backlinks()
        return flask.redirect(note.url)

@app.route("/move/<path:path>", methods=["GET", "POST"])
def move(path):
    "Move the given note to a new supernote."
    try:
        note = get_note(path)
    except KeyError:
        flash_error(f"No such note: '{path}'")
        return flask.redirect(flask.url_for("note", path=os.path.dirname(path)))

    if flask.request.method == "GET":
        if note is ROOT:
            flash_error("Cannot move root note.")
            return flask.redirect(note.url)
        return flask.render_template("move.html", note=note)

    elif flask.request.method == "POST":
        supernote = flask.request.form.get("supernote") or ""
        if supernote.startswith("[["):
            supernote = supernote[2:]
        if supernote.endswith("]]"):
            supernote = supernote[:-2]
        try:
            supernote = get_note(supernote)
        except KeyError:
            flash_error(f"No such supernote: '{supernote}'")
        try:
            note.move(supernote)
        except ValueError as error:
            flash_error(error)
        check_recent_ordered()
        check_synced_filesystem()
        check_synced_memory()
        check_backlinks()
        return flask.redirect(note.url)

@app.route("/delete/<path:path>", methods=["POST"])
def delete(path):
    "Delete the given note."
    try:
        note = get_note(path)
    except KeyError:
        flash_error(f"No such note: '{path}'")
        return flask.redirect(flask.url_for("note", path=os.path.dirname(path)))
    try:
        note.delete()
    except ValueError as error:
        flash_error(error)
        return flask.redirect(note.url)
    check_recent_ordered()
    check_synced_filesystem()
    check_synced_memory()
    check_backlinks()
    return flask.redirect(note.supernote.url)

@app.route("/star/<path:path>", methods=["POST"])
def star(path):
    "Toggle the star state of the note for the path."
    try:
        note = get_note(path)
    except KeyError:
        flash_error(f"No such note: '{path}'")
        return flask.redirect(flask.url_for("note", path=os.path.dirname(path)))
    note.star()
    return flask.redirect(note.url)

@app.route("/hashtag/<word>")
def hashtag(word):
    notes = [get_note(p) for p in HASHTAGS.get(word, [])]
    return flask.render_template("hashtag.html", word=word, notes=notes)

@app.route("/search")
def search():
    terms = flask.request.values.get("terms") or ""
    if terms:
        terms = terms.strip()
        if (terms[0] == '"' and terms[-1] == '"') or \
           (terms[0] == "'" and terms[-1] == "'"):
            terms = [terms[1:-1]]   # Quoted term; search as a whole
        else:
            terms = terms.split()
        terms.sort(key=lambda t: len(t), reverse=True)
        notes = []
        traverser = ROOT.traverse()
        next(traverser)             # Skip root note.
        timer = Timer()
        for note in traverser:
            for term in terms:
                if term not in note: break
            else:
                notes.append(note)
        flash_message(f"Search {timer}")
    else:
        notes = []
    return flask.render_template("search.html",
                                 notes=sorted(notes),
                                 terms=flask.request.values.get("terms"))

@app.route("/notebook/<title>")
def notebook(title=None):
    "Change to another notebook."
    for notebook in flask.current_app.config["NOTEBOOKS"]:
        if not os.path.isdir(notebook): continue
        if not (os.access(notebook, os.R_OK) and 
                os.access(notebook, os.W_OK)):
            flash_error("You may not read and write the directory.")
            break
        notebook_title = os.path.basename(notebook)
        if notebook_title == title:
            flask.current_app.config["NOTEBOOK_DIRPATH"] = notebook
            flask.current_app.config["NOTEBOOK_TITLE"] = notebook_title
            flask.current_app.config["NOTEBOOKS"].remove(notebook)
            flask.current_app.config["NOTEBOOKS"].insert(0, notebook)
            write_settings()    # Keep this state for next session.
            setup()
            break
    else:
        flash_error(f"No such notebook '{title}'.")
    return flask.redirect(flask.url_for("home"))

@app.route("/notebook", methods=["GET", "POST"])
def add_notebook():
    "Add a directory as a notebook. It must exist."
    if flask.request.method == "GET":
        return flask.render_template("notebook.html")

    elif flask.request.method == "POST":
        try:
            dirpath = flask.request.form["notebook"]
        except KeyError:
            flash_error("No directory path given.")
            return flask.redirect(flask.url_for("home"))
        dirpath = os.path.expanduser(dirpath)
        dirpath = os.path.expandvars(dirpath)
        dirpath = os.path.normpath(dirpath)
        if not os.path.isabs(dirpath):
            dirpath = os.path.join(os.path.expanduser("~"), dirpath)
        title = os.path.basename(dirpath)
        # If the notebook exists, just go to it.
        if dirpath in flask.current_app.config["NOTEBOOKS"]:
            return flask.redirect(flask.url_for("notebook", title=title))
        # Directory exists; add it as a notebook and go to it.
        if os.path.isdir(dirpath):
            flask.current_app.config["NOTEBOOKS"].append(dirpath)
            write_settings()
            return flask.redirect(flask.url_for("notebook", title=title))
        # The path is a non-directory, or does not exist.
        if os.path.exists(dirpath):
            flash_error(f"The path '{dirpath}' does not specify a directory.")
        else:
            flash_error(f"The directory '{dirpath}' does not exist.")
        return flask.redirect(flask.url_for("home"))


if __name__ == "__main__":
    settings = get_settings()
    app.config.from_mapping(settings)
    app.run(debug=settings["DEBUG"])
