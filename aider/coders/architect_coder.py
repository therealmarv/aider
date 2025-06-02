import re
from pathlib import Path

from .architect_prompts import ArchitectPrompts
from .ask_coder import AskCoder
from .base_coder import Coder


class ArchitectCoder(AskCoder):
    edit_format = "architect"
    gpt_prompts = ArchitectPrompts()
    auto_accept_architect = False

    def reply_completed(self):
        architect_instructions = self.partial_response_content

        if not architect_instructions or not architect_instructions.strip():
            return

        # Initialize a list to hold instructions for the editor
        editor_instructions_lines = []
        # Set to record files deleted by the architect's direct command
        files_deleted_by_architect = set()

        delete_pattern = re.compile(r"^DELETE FILE:\s*(.*)$", re.IGNORECASE)

        for line in architect_instructions.splitlines():
            match = delete_pattern.match(line.strip())
            if match:
                file_to_delete_str = match.group(1).strip()
                if not file_to_delete_str:
                    self.io.tool_warning("Architect specified `DELETE FILE:` with no path.")
                    editor_instructions_lines.append(line) # Pass malformed line to editor
                    continue

                abs_file_to_delete = self.abs_root_path(file_to_delete_str)
                rel_file_to_delete = self.get_rel_fname(abs_file_to_delete)

                if Path(abs_file_to_delete).exists():
                    self.io.tool_output(f"Deleting {rel_file_to_delete} as per architect's instruction.")
                    try:
                        if not self.dry_run:
                            if self.repo:
                                self.repo.repo.git.rm(str(abs_file_to_delete))
                            else:
                                Path(abs_file_to_delete).unlink()
                        
                        files_deleted_by_architect.add(rel_file_to_delete)

                    except Exception as e:
                        self.io.tool_error(f"Error trying to delete {rel_file_to_delete}: {e}")
                        editor_instructions_lines.append(line) # Pass problematic line to editor
                else:
                    self.io.tool_warning(f"Architect requested deletion of non-existent file: {rel_file_to_delete}")
                    files_deleted_by_architect.add(rel_file_to_delete) # Still note intent
            else:
                editor_instructions_lines.append(line)
        
        # Update coder's record of edited files for commit purposes
        if files_deleted_by_architect:
            self.aider_edited_files.update(files_deleted_by_architect)

        editor_content_for_llm = "\n".join(editor_instructions_lines).strip()

        if not editor_content_for_llm and not files_deleted_by_architect:
             # No actual editor instructions and no files deleted, maybe just a comment from architect
            return

        if not editor_content_for_llm and files_deleted_by_architect:
            # Only file deletions were performed, no further editor actions needed
            self.move_back_cur_messages(f"Ok, I have deleted {', '.join(files_deleted_by_architect)}.")
            # auto_commit will be handled by the main loop if applicable
            return


        # Proceed with editor if there are remaining instructions
        if not self.auto_accept_architect and not self.io.confirm_ask("Edit the files based on architect's plan?"):
            return

        kwargs = dict()
        editor_model = self.main_model.editor_model or self.main_model
        kwargs["main_model"] = editor_model
        kwargs["edit_format"] = self.main_model.editor_edit_format
        kwargs["suggest_shell_commands"] = False
        kwargs["map_tokens"] = 0
        kwargs["total_cost"] = self.total_cost
        kwargs["cache_prompts"] = False
        kwargs["num_cache_warming_pings"] = 0
        kwargs["summarize_from_coder"] = False


        new_kwargs = dict(io=self.io, from_coder=self)
        new_kwargs.update(kwargs)

        editor_coder = Coder.create(**new_kwargs)
        editor_coder.cur_messages = []
        editor_coder.done_messages = []

        if self.verbose:
            editor_coder.show_announcements()

        editor_coder.run(with_message=editor_content_for_llm, preproc=False)

        # Consolidate messages about changes
        final_message_parts = []
        if files_deleted_by_architect:
            final_message_parts.append(f"deleted {', '.join(files_deleted_by_architect)}")
        if editor_coder.aider_edited_files: # Check if editor actually made changes
            # Filter out files already deleted by architect from editor's list
            editor_changed_files = [f for f in editor_coder.aider_edited_files if f not in files_deleted_by_architect]
            if editor_changed_files:
                final_message_parts.append(f"made other changes to {', '.join(editor_changed_files)}")
        
        if final_message_parts:
            self.move_back_cur_messages(f"Ok, I have {', and '.join(final_message_parts)}.")
        elif not editor_content_for_llm and not files_deleted_by_architect: # Should be caught earlier
            pass
        else: # No files deleted, and editor made no changes (or only proposed)
             self.move_back_cur_messages("Ok, I've processed the architect's instructions.")


        self.total_cost = editor_coder.total_cost
        # Combine deleted files with files edited by the editor for commit
        self.aider_edited_files.update(editor_coder.aider_edited_files)
        # Carry over commit hashes if any were made by the editor
        self.aider_commit_hashes.update(editor_coder.aider_commit_hashes)
        if hasattr(editor_coder, 'last_aider_commit_hash') and editor_coder.last_aider_commit_hash:
            self.last_aider_commit_hash = editor_coder.last_aider_commit_hash
            self.last_aider_commit_message = editor_coder.last_aider_commit_message
