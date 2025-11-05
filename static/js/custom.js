document.addEventListener('DOMContentLoaded', () => {
    const forms = document.querySelectorAll('form');
    forms.forEach(form => {
      form.addEventListener('submit', (event) => {
        if (!form.checkValidity()) {
          event.preventDefault();
          event.stopPropagation();
          alert('Preencha todos os campos corretamente antes de salvar!');
        }
        form.classList.add('was-validated');
      }, false);
    });
  });
  