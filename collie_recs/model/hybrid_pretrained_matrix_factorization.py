from collections import OrderedDict
import copy
from functools import partial
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import joblib
import numpy as np
import pandas as pd
import torch
from torch import nn
import torch.nn.functional as F
from torch.optim.lr_scheduler import ReduceLROnPlateau

from collie_recs.config import DATA_PATH
from collie_recs.model.base import (BasePipeline,
                                    interactions_like_input,
                                    ScaledEmbedding)
from collie_recs.model.matrix_factorization import MatrixFactorizationModel
from collie_recs.utils import get_init_arguments


class HybridPretrainedModel(BasePipeline):
    """
    Training pipeline for a hybrid recommendation model.

    ``HybridPretrainedModel`` models contain dense layers that process item metadata, concatenate
    this embedding with the user and item embeddings copied from a trained
    ``MatrixFactorizationModel``, and send this concatenated embedding through more dense layers to
    output a single float ranking / rating.

    All ``HybridPretrainedModel`` instances are subclasses of the ``LightningModule`` class
    provided by PyTorch Lightning. This means to train a model, you will need a
    ``collie_recs.model.CollieTrainer`` object, but the model can be saved and loaded without this
    ``Trainer`` instance. Example usage may look like:

    .. code-block:: python

        from collie_recs.model import CollieTrainer, HybridPretrainedModel, MatrixFactorizationModel


        # instantiate and fit a ``MatrixFactorizationModel`` as expected
        mf_model = MatrixFactorizationModel(train=train)
        mf_trainer = CollieTrainer(mf_model)
        mf_trainer.fit(mf_model)

        hybrid_model = HybridPretrainedModel(train=train,
                                             item_metadata=item_metadata,
                                             trained_model=mf_model)
        hybrid_trainer = CollieTrainer(hybrid_model)
        hybrid_trainer.fit(hybrid_model)
        hybrid_model.eval()

        # do evaluation as normal with ``hybrid_model``

        hybrid_model.save_model(path='model')
        new_hybrid_model = HybridPretrainedModel(load_model_path='model')

        # do evaluation as normal with ``new_hybrid_model``

    Parameters
    ----------
    train: ``collie_recs.interactions`` object
        Data loader for training data. If an ``Interactions`` object is supplied, an
        ``InteractionsDataLoader`` will automatically be instantiated with ``shuffle=True``. Note
        that when the model class is saved, datasets will NOT be saved as well
    val: ``collie_recs.interactions`` object
        Data loader for validation data. If an ``Interactions`` object is supplied, an
        ``InteractionsDataLoader`` will automatically be instantiated with ``shuffle=False``. Note
        that when the model class is saved, datasets will NOT be saved as well
    item_metadata: torch.tensor, pd.DataFrame, or np.array, 2-dimensional
        The shape of the item metadata should be (num_items x metadata_features), and each item's
        metadata should be available when indexing a row by an item ID
    trained_model: ``collie_recs.model.MatrixFactorizationModel``
        Previously trained ``MatrixFactorizationModel`` model to extract embeddings from
    metadata_layers_dims: list
        List of linear layer dimensions to apply to the metadata only, starting with the dimension
        directly following ``metadata_features`` and ending with the dimension to concatenate with
        the item embeddings
    combined_layers_dims: list
        List of linear layer dimensions to apply to the concatenated item embeddings and item
        metadata, starting with the dimension directly following the shape of
        ``item_embeddings + metadata_features`` and ending with the dimension before the final
        linear layer to dimension 1
    freeze_embeddings: bool
        When initializing the model, whether or not to freeze ``trained_model``'s embeddings
    dropout_p: float
        Probability of dropout
    lr: float
        Model learning rate
    lr_scheduler_func: torch.optim.lr_scheduler
        Learning rate scheduler to use during fitting
    weight_decay: float
        Weight decay passed to the optimizer, if optimizer permits
    optimizer: torch.optim or str
        If a string, one of the following supported optimizers:

        * ``'sgd'`` (for ``torch.optim.SGD``)

        * ``'adam'`` (for ``torch.optim.Adam``)

    loss: function or str
        If a string, one of the following implemented losses:

        * ``'bpr'`` / ``'adaptive_bpr'``

        * ``'hinge'`` / ``'adaptive_hinge'``

        * ``'warp'``

        If ``train.num_negative_samples > 1``, the adaptive loss version will automatically be used
    metadata_for_loss: dict
        Keys should be strings identifying each metadata type that match keys in
        ``metadata_weights``. Values should be a ``torch.tensor`` of shape (num_items x 1). Each
        tensor should contain categorical metadata information about items (e.g. a number
        representing the genre of the item)
    metadata_for_loss_weights: dict
        Keys should be strings identifying each metadata type that match keys in ``metadata``.
        Values should be the amount of weight to place on a match of that type of metadata, with
        the sum of all values ``<= 1``.
        e.g. If ``metadata_for_loss_weights = {'genre': .3, 'director': .2}``, then an item is:

        * a 100% match if it's the same item,

        * a 50% match if it's a different item with the same genre and same director,

        * a 30% match if it's a different item with the same genre and different director,

        * a 20% match if it's a different item with a different genre and same director,

        * a 0% match if it's a different item with a different genre and different director,
          which is equivalent to the loss without any partial credit
    load_model_path: str or Path
        To load a previously-saved model, pass in path to output of ``model.save_model()`` method.
        If ``None``, will initialize model as normal
    map_location: str or torch.device
        If ``load_model_path`` is provided, device specifying how to remap storage locations when
        ``torch.load``-ing the state dictionary

    """
    def __init__(self,
                 train: interactions_like_input = None,
                 val: interactions_like_input = None,
                 item_metadata: Union[torch.tensor, pd.DataFrame, np.array] = None,
                 trained_model: MatrixFactorizationModel = None,
                 metadata_layers_dims: Optional[List[int]] = None,
                 combined_layers_dims: List[int] = [128, 64, 32],
                 freeze_embeddings: bool = True,
                 dropout_p: float = 0.0,
                 lr: float = 1e-3,
                 lr_scheduler_func: Optional[Callable] = partial(ReduceLROnPlateau,
                                                                 patience=1,
                                                                 verbose=True),
                 weight_decay: float = 0.0,
                 optimizer: Union[str, Callable] = 'adam',
                 loss: Union[str, Callable] = 'hinge',
                 metadata_for_loss: Optional[Dict[str, torch.tensor]] = None,
                 metadata_for_loss_weights: Optional[Dict[str, float]] = None,
                 # y_range: Optional[Tuple[float, float]] = None,
                 load_model_path: Optional[str] = None,
                 map_location: Optional[str] = None):
        item_metadata_num_cols = None
        if load_model_path is None:
            if trained_model is None:
                raise ValueError('Must provide ``trained_model`` for ``HybridPretrainedModel``.')

            if item_metadata is None:
                raise ValueError('Must provide item metadata for ``HybridPretrainedModel``.')
            elif isinstance(item_metadata, pd.DataFrame):
                item_metadata = torch.from_numpy(item_metadata.to_numpy())
            elif isinstance(item_metadata, np.ndarray):
                item_metadata = torch.from_numpy(item_metadata)

            item_metadata = item_metadata.float()

            item_metadata_num_cols = item_metadata.shape[1]

        super().__init__(**get_init_arguments(),
                         item_metadata_num_cols=item_metadata_num_cols)

    def _load_model_init_helper(self, load_model_path: str, map_location: str, **kwargs) -> None:
        self.item_metadata = joblib.load(os.path.join(load_model_path, 'metadata.pkl'))
        super()._load_model_init_helper(load_model_path=os.path.join(load_model_path, 'model.pth'),
                                        map_location=map_location)

    def _setup_model(self, **kwargs) -> None:
        """
        Method for building model internals that rely on the data passed in.

        This method will be called after ``prepare_data``.

        """
        if self.hparams.load_model_path is None:
            if not hasattr(self, '_trained_model'):
                self._trained_model = kwargs.pop('trained_model')
            if not hasattr(self, 'item_metadata'):
                self.item_metadata = kwargs.pop('item_metadata')

            # we are not loading in a model, so we will create a new model from scratch
            # we don't want to modify the ``trained_model``'s weights, so we deep copy
            self.embeddings = nn.Sequential(
                copy.deepcopy(self._trained_model.user_embeddings),
                copy.deepcopy(self._trained_model.item_embeddings)
            )

            if self.hparams.freeze_embeddings:
                self.freeze_embeddings()
            else:
                self.unfreeze_embeddings()

            # save hyperparameters that we need to be able to rebuilt the embedding layers on load
            self.hparams.user_num_embeddings = self.embeddings[0].num_embeddings
            self.hparams.user_embeddings_dim = self.embeddings[0].embedding_dim
            self.hparams.item_num_embeddings = self.embeddings[1].num_embeddings
            self.hparams.item_embeddings_dim = self.embeddings[1].embedding_dim
        else:
            # assume we are loading in a previously-saved model
            # set up dummy embeddings with the correct dimensions so we can load weights in
            self.embeddings = nn.Sequential(
                ScaledEmbedding(self.hparams.user_num_embeddings, self.hparams.user_embeddings_dim),
                ScaledEmbedding(self.hparams.item_num_embeddings, self.hparams.item_embeddings_dim)
            )

        self.dropout = nn.Dropout(p=self.hparams.dropout_p)

        # set up metadata-only layers
        metadata_output_dim = self.hparams.item_metadata_num_cols
        self.metadata_layers = None
        if self.hparams.metadata_layers_dims is not None:
            metadata_layers_dims = (
                [self.hparams.item_metadata_num_cols] + self.hparams.metadata_layers_dims
            )
            self.metadata_layers = [
                nn.Linear(metadata_layers_dims[idx - 1], metadata_layers_dims[idx])
                for idx in range(1, len(metadata_layers_dims))
            ]
            for i, layer in enumerate(self.metadata_layers):
                nn.init.xavier_normal_(self.metadata_layers[i].weight)
                self.add_module('metadata_layer_{}'.format(i), layer)

            metadata_output_dim = metadata_layers_dims[-1]

        # set up combined layers
        combined_dimension_input = (
            self.hparams.user_embeddings_dim
            + self.hparams.item_embeddings_dim
            + metadata_output_dim
        )
        combined_layers_dims = [combined_dimension_input] + self.hparams.combined_layers_dims + [1]
        self.combined_layers = [
            nn.Linear(combined_layers_dims[idx - 1], combined_layers_dims[idx])
            for idx in range(1, len(combined_layers_dims))
        ]
        for i, layer in enumerate(self.combined_layers):
            nn.init.xavier_normal_(self.combined_layers[i].weight)
            self.add_module('combined_layer_{}'.format(i), layer)

    def forward(self, users: torch.tensor, items: torch.tensor) -> torch.tensor:
        """
        Forward pass through the model.

        Parameters
        ----------
        users: tensor, 1-d
            Array of user indices
        items: tensor, 1-d
            Array of item indices

        Returns
        -------
        preds: tensor, 1-d
            Predicted ratings or rankings

        """
        # TODO: remove self.device and let lightning do it
        metadata_output = self.item_metadata[items, :].to(self.device)
        if self.metadata_layers is not None:
            for metadata_nn_layer in self.metadata_layers:
                metadata_output = self.dropout(
                    F.leaky_relu(
                        metadata_nn_layer(metadata_output)
                    )
                )

        combined_output = torch.cat((self.embeddings[0](users),
                                     self.embeddings[1](items),
                                     metadata_output), 1)
        for combined_nn_layer in self.combined_layers[:-1]:
            combined_output = self.dropout(
                F.leaky_relu(
                    combined_nn_layer(combined_output)
                )
            )

        pred_scores = self.combined_layers[-1](combined_output)

        return pred_scores.squeeze()

    def _get_item_embeddings(self) -> np.array:
        """Get item embeddings."""
        # TODO: update this to get the embeddings post-MLP
        return self.embeddings[1](
            torch.arange(self.hparams.num_items, device=self.device)
        ).detach().cpu()

    def freeze_embeddings(self) -> None:
        """Remove gradient requirement from the embeddings."""
        self.embeddings[0].weight.requires_grad = False
        self.embeddings[1].weight.requires_grad = False

    def unfreeze_embeddings(self) -> None:
        """Require gradients for the embeddings."""
        self.embeddings[0].weight.requires_grad = True
        self.embeddings[1].weight.requires_grad = True

    def save_model(self,
                   path: Union[str, Path] = os.path.join(DATA_PATH / 'model'),
                   overwrite: bool = False) -> None:
        """
        Save the model's state dictionary, hyperparameters, and item metadata.

        While PyTorch Lightning offers a way to save and load models, there are two main reasons
        for overriding these:

        1) To properly save and load a model requires the ``Trainer`` object, meaning that all
           deployed models will require Lightning to run the model, which is not actually needed
           for inference.

        2) In the v0.8.4 release, loading a model back in leads to a ``RuntimeError`` unable to load
           in weights.

        Parameters
        ----------
        path: str or Path
            Directory path to save model and data files
        overwrite: bool
            Whether or not to overwrite existing data

        """
        path = str(path)

        if os.path.exists(path):
            if os.listdir(path) and overwrite is False:
                raise ValueError(f'Data exists in ``path`` at {path} and ``overwrite`` is False.')

        Path(path).mkdir(parents=True, exist_ok=True)
        joblib.dump(self.item_metadata, os.path.join(path, 'metadata.pkl'))

        # preserve ordering while extracting the state dictionary without the ``_trained_model``
        # component
        state_dict_keys_to_save = [
            k for k, _ in self.state_dict().items() if '_trained_model' not in k
        ]
        state_dict_vals_to_save = [
            v for k, v in self.state_dict().items() if '_trained_model' not in k
        ]
        state_dict_to_save = OrderedDict(zip(state_dict_keys_to_save, state_dict_vals_to_save))

        dict_to_save = {'state_dict': state_dict_to_save, 'hparams': self.hparams}
        torch.save(dict_to_save, os.path.join(path, 'model.pth'))

    def load_from_hybrid_model(self, hybrid_model) -> None:
        """
        Copy hyperparameters and state dictionary from an existing ``HybridPretrainedModel``
        instance.

        This is particularly useful for creating another PyTorch Lightning trainer object to
        fine-tune copied-over embeddings from a ``MatrixFactorizationModel`` instance.

        Parameters
        ----------
        hybrid_model: ``collie_recs.model.HybridPretrainedModel``
            HybridPretrainedModel containing hyperparameters and state dictionary to copy over

        """
        for key, value in hybrid_model.hparams.items():
            self.hparams[key] = value

        self._setup_model()
        self.load_state_dict(state_dict=hybrid_model.state_dict())
        self.eval()
