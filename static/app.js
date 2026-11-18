function webPrinterPage(backendReady) {
  return {
    backendReady,
    sendingPrint: false,
    uploading: false,
    uploadProgress: 0,
    uploadError: '',
    uploadSuccessMessage: '',
    selectedCount: 0,
    stagedFiles: [],
    imageMode: 'fit',
    autoRefreshEnabled: true,
    lastUpdateTimestamp: '--',
    autoRefreshTimerId: null,

    async loadStagedFiles() {
      try {
        const response = await fetch('/uploads', { cache: 'no-store' });
        if (!response.ok) throw new Error('Impossibile recuperare i file.');
        const data = await response.json();
        this.stagedFiles = data.files.map((file) => ({
          id: file.id,
          name: file.name,
          previewUrl: file.preview_url,
          type: file.type,
          requiresPassword: file.requires_password,
          password: '',
          expanded: false,
          height: 280,
          landscape: false,
        }));
      } catch (error) {
        this.uploadError = error.message || 'Errore durante il recupero dei file.';
      }
    },

    handleFileSelection(event) {
      this.selectedCount = event.target.files.length;
      this.uploadError = '';
      this.uploadSuccessMessage = '';
    },

    generateUploadId() {
      if (window.crypto && window.crypto.randomUUID) {
        return window.crypto.randomUUID().replace(/-/g, '');
      }
      return Date.now().toString(16) + '_' + Math.random().toString(16).slice(2);
    },

    getChunkSize(fileSize) {
      if (fileSize > 50 * 1024 * 1024) return 2 * 1024 * 1024;
      if (fileSize > 5 * 1024 * 1024) return 1024 * 1024;
      return 512 * 1024;
    },

    async uploadFile(file, progress) {
      const uploadId = this.generateUploadId();
      const chunkSize = this.getChunkSize(file.size);
      const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
      try {
        for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex++) {
          const start = chunkIndex * chunkSize;
          const end = Math.min(start + chunkSize, file.size);
          const payload = new FormData();
          payload.append('upload_id', uploadId);
          payload.append('chunk_index', String(chunkIndex));
          payload.append('total_chunks', String(totalChunks));
          payload.append('original_name', file.name);
          payload.append('mime_type', file.type || 'application/octet-stream');
          payload.append('chunk', file.slice(start, end), file.name);

          const response = await fetch('/uploads/chunk', { method: 'POST', body: payload });
          const data = await response.json();
          if (!response.ok) {
            throw new Error(data.detail || 'Errore durante il caricamento.');
          }
          progress(end || 1);
          if (data.completed) {
            return {
              id: data.upload_id || uploadId,
              name: data.filename || file.name,
              previewUrl: data.preview_url || '',
              type: data.file_type || '',
              requiresPassword: Boolean(data.pdf_requires_password),
              password: '',
              expanded: false,
              height: 280,
              landscape: false,
            };
          }
        }
        throw new Error('Caricamento incompleto.');
      } catch (error) {
        try {
          await fetch('/uploads/' + encodeURIComponent(uploadId), { method: 'DELETE' });
        } catch (_) {
          // The server may have discarded the failed upload already.
        }
        throw error;
      }
    },

    async uploadSelectedFiles() {
      if (!this.backendReady || this.uploading || this.sendingPrint) return;
      const selected = Array.from(this.$refs.fileInput.files || []);
      if (!selected.length) {
        this.uploadError = 'Seleziona almeno un file.';
        return;
      }
      if (this.stagedFiles.length + selected.length > 50) {
        this.uploadError = 'Puoi caricare al massimo 50 file per lotto.';
        return;
      }

      this.uploading = true;
      this.uploadProgress = 0;
      this.uploadError = '';
      this.uploadSuccessMessage = '';
      const completed = new Array(selected.length);
      const failures = [];
      const uploadedBytes = selected.map(() => 0);
      const totalBytes = selected.reduce((sum, file) => sum + Math.max(1, file.size), 0);
      let nextIndex = 0;

      const worker = async () => {
        while (nextIndex < selected.length) {
          const index = nextIndex++;
          const file = selected[index];
          try {
            completed[index] = await this.uploadFile(file, (bytes) => {
              uploadedBytes[index] = bytes;
              this.uploadProgress = Math.round(
                uploadedBytes.reduce((sum, value) => sum + value, 0) / totalBytes * 100
              );
            });
          } catch (error) {
            failures.push(file.name + ': ' + (error.message || 'Upload non riuscito.'));
          }
        }
      };

      try {
        await Promise.all(
          Array.from({ length: Math.min(3, selected.length) }, () => worker())
        );
        const successes = completed.filter(Boolean);
        this.stagedFiles.push(...successes);
        if (successes.length) {
          this.uploadSuccessMessage = successes.length + ' file caricati.';
        }
        if (failures.length) this.uploadError = failures.join(' | ');
      } finally {
        this.uploading = false;
        this.uploadProgress = 0;
        this.$refs.fileInput.value = '';
        this.selectedCount = 0;
      }
    },

    async deleteUploadedFile(file) {
      if (this.uploading || this.sendingPrint) return;
      try {
        const response = await fetch(
          '/uploads/' + encodeURIComponent(file.id), { method: 'DELETE' }
        );
        if (!response.ok && response.status !== 404) {
          throw new Error('Impossibile eliminare il file.');
        }
        this.stagedFiles = this.stagedFiles.filter((item) => item.id !== file.id);
        this.uploadSuccessMessage = '';
      } catch (error) {
        this.uploadError = error.message || 'Impossibile eliminare il file.';
      }
    },

    hasPreview(file) {
      return ['image', 'pdf', 'text'].includes(file.type);
    },

    togglePreview(file) {
      file.expanded = !file.expanded;
    },

    startPreviewResize(event, file) {
      if (!file.expanded) return;
      if (event.button !== undefined && event.button !== 0) return;
      event.preventDefault();

      const startY = event.clientY;
      const startHeight = file.height;
      const maxHeight = Math.max(320, Math.floor(window.innerHeight * 0.8));
      const onPointerMove = (moveEvent) => {
        file.height = Math.max(180, Math.min(maxHeight, startHeight + moveEvent.clientY - startY));
      };
      const onPointerUp = () => {
        window.removeEventListener('pointermove', onPointerMove);
        window.removeEventListener('pointerup', onPointerUp);
        window.removeEventListener('pointercancel', onPointerUp);
      };
      window.addEventListener('pointermove', onPointerMove);
      window.addEventListener('pointerup', onPointerUp);
      window.addEventListener('pointercancel', onPointerUp);
    },

    pdfPasswords() {
      return JSON.stringify(Object.fromEntries(
        this.stagedFiles
          .filter((file) => file.requiresPassword)
          .map((file) => [file.id, file.password])
      ));
    },

    handlePrintSubmit(event) {
      if (!this.stagedFiles.length || this.uploading || this.sendingPrint ||
          this.stagedFiles.some((file) => file.requiresPassword && !file.password)) {
        event.preventDefault();
        return;
      }
      this.sendingPrint = true;
    },

    clearUploadedFiles() {
      this.stagedFiles = [];
      this.uploadError = '';
      this.uploadSuccessMessage = '';
      this.imageMode = 'fit';
      this.$refs.fileInput.value = '';
      this.selectedCount = 0;
    },

    handleBeforePrintRequest(event) {
      if (event.detail && event.detail.elt && event.detail.elt.id === 'print-form') {
        this.sendingPrint = true;
      }
    },

    handleAfterPrintRequest(event) {
      if (event.detail && event.detail.elt && event.detail.elt.id === 'print-form') {
        this.sendingPrint = false;
      }
    },

    setupPrinterMonitor() {
      const panel = this.$refs.printerMonitorPanel;
      if (!panel || !window.htmx) return;
      document.body.addEventListener('htmx:afterSwap', (event) => {
        if (event.target === panel) this.lastUpdateTimestamp = this.formatLastUpdate();
      });
      this.autoRefreshTimerId = window.setInterval(() => {
        if (this.autoRefreshEnabled) window.htmx.trigger(panel, 'monitor-refresh');
      }, 30000);
    },

    toggleAutoRefresh() {
      this.autoRefreshEnabled = !this.autoRefreshEnabled;
      if (this.autoRefreshEnabled && this.$refs.printerMonitorPanel && window.htmx) {
        window.htmx.trigger(this.$refs.printerMonitorPanel, 'monitor-refresh');
      }
    },

    formatLastUpdate() {
      return new Date().toLocaleString('it-IT');
    },
  };
}
